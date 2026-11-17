# Compaction

> Part of the [linch-rust architecture guide](./README.md).

Compaction shortens the provider view of a long run. A summary folds the
older part of the view into one message; micro-compaction clears old tool
results with no model call. A run compacts at the start of a turn, before
the primary provider call, when the view is due, and out of turn, forced,
when the provider refused the primary call's prompt as longer than the
model's context window. The result is always a logged projection on the
session's append-only log: the model sees a shorter view, and the full
history keeps every message. The pure parts are in
`crates/linch-core/src/compaction.rs`; the run's part, with its barriers, is
`crates/linch-core/src/runner/compact.rs`. Use this page when you change when
a run compacts, what the summary holds, or how a run recovers a compaction.

Truncation recovery, the other size-driven retry, is described at the end
([cut-off answers](#cut-off-answers)). How to use all of it from Python is in
[Memory](../usage/memory.md).

Python Linch has the same shape ([compaction](../../../docs/architecture/compaction.md)).
What differs in Rust:

- `CompactionSpec` is data. The ladder is fixed: micro-compaction when
  `micro` is set, then a summary. There is no custom strategy; the prompt is
  the summary's only hook (`compaction.rs:143-190`). Python's
  `DetailedCompaction` is a prompt, `DETAILED_PROMPT` (`compaction.rs:46-83`).
- The reactive path (a context-length refusal) runs only when the spec has
  compaction. Python always has a strategy to fall back on; here a refusal
  without compaction ends the run.
- The size estimate is built in: about four characters per token and 1000 per
  image (`compaction.rs:235-268`). There is no pluggable token estimator.
- A paid summary is kept whole in the `Compacted` checkpoint, not as a
  hash-only recipe. A resume appends it without another provider call
  (`crates/linch-core/src/run.rs:147-154`, `runner/compact.rs:303-318`).
- The projection is always written to the session store as a log entry
  (`runner/write.rs:83-98`). There is no store detection and no opt-in.

## Turning it on

`AgentSpec::compaction` is `None` by default, which never compacts
(`crates/linch-core/src/agent/spec.rs:67`). Set a `CompactionSpec`
(`compaction.rs:143-190`):

| Field | Default | Meaning |
|---|---|---|
| `threshold_tokens` | none | Compact when the estimated view reaches this many tokens. Without it, compact near the provider's context window (below) |
| `keep_recent_turns` | 10 | Assistant turns kept verbatim after the summary; at least 1 |
| `prompt` | `DEFAULT_PROMPT` | Instructions for the summary call; they ride in its last user message |
| `max_output_tokens` | 4096 | Most tokens the summary may use |
| `micro` | false | Clear old tool results before a summary ([below](#micro-compaction)) |
| `keep_user_tokens` | 15000 | Tokens of folded user messages the checkpoint carries verbatim ([below](#the-checkpoint)); 0 carries none; off the wire at the default |
| `max_forced` | 3 | Most summaries one run makes for a context-length refusal; 0 never ([below](#context-length-recovery)) |

`micro` and `max_forced` are left out of the serialized spec at their
defaults, so the contract digest of a spec written before they existed is
unchanged. `CompactionSpec::new(n)` sets the threshold and leaves the rest
at their defaults (`compaction.rs:207-216`). `AgentBuilder::build` refuses a
zero threshold, a zero `keep_recent_turns` or a zero `max_output_tokens`
with `BuildError::Invalid` (`compaction.rs:218-233`, `agent/builder.rs:282`).

The spec is part of the run contract, so changing it makes agents with the old
and the new setting incompatible: a resume of an old run is refused
(`agent/spec.rs`, `runner/start.rs`).

## When a run compacts

A turn starts at `Step::Compact` when the spec has compaction, and at
`Step::Dispatch` otherwise (`runner/turn.rs:316`). `compact` goes to the
primary call without doing anything when any of these hold
(`runner/compact.rs:73-129`):

1. The run was cancelled: it aborts instead.
2. `max_turns` or the budget already allows no more calls. The primary call
   ends the run with its own terminal.
3. The view is not due.
4. `keep_from` finds no point to fold. The view needs at least one assistant
   turn between its first message and the kept turns (`compaction.rs:289-313`).
   A one-prompt first turn never compacts.

Due is decided by `due` (`compaction.rs:270-288`):

- With `threshold_tokens`, the estimate is at or above it.
- Without it, the estimate plus an output reserve reaches 80% of the model's
  context window. The reserve is the spec's `max_output_tokens` when set and
  not 0, else a fifth of the window, at least 1 and at most 32768. The window
  is `Provider::context_window` for the spec's model
  ([providers](./providers.md)).

The estimate counts text, thinking, catalog notices, tool inputs and tool
results; images cost a fixed amount; signatures and redacted reasoning are not
counted (`compaction.rs:235-268`).

`keep_from` is the `keep_recent_turns`-th assistant message from the end of
the view. The kept part starts at that message, so a tool result is never cut
off from its call (`compaction.rs:289-313`).

## Micro-compaction

With `micro`, a due view is cleared first (`runner/compact.rs:110-115`).
`clear_tool_results` replaces every tool result before the kept turns with
`CLEARED_RESULT`, `[tool result elided to save context]`, unless it is
already no longer than the placeholder and holds no image (Python's test,
`compaction.rs:322-359`). Every `tool_use_id` keeps its result, and messages
that do not change are shared, not copied.

If the cleared view is no longer due, `clear` records it and the turn goes
on with its primary call (`runner/compact.rs:359-402`):

1. `pre_compact` runs with strategy `micro`.
2. The `compaction` event (strategy `micro`, `messages_before` equal to
   `messages_after`) is staged with a held `Cleared { before, attempt }`
   checkpoint (`run.rs:159-168`).
3. `project` flushes the held checkpoint and its event, then appends the
   projection, reason `micro-compaction`, to the session.
4. `post_compact` runs.

That is one run write and one session write, and no provider call. If the
cleared view is still due, the summary follows, and it reads the folded part
with its tool results already cleared, as Python's does after its micro step
(`runner/compact.rs:135-142`).

Clearing changes messages in the middle of the view, so the provider's cached
prefix ends at the first cleared result. That is why `micro` is off by
default.

## The summary call

If the view is due, the run builds a summary request (`compaction.rs`
`summary_request`, `runner/compact.rs`). It is the request the turn's primary
call would send, plus one message:

- The system blocks, the tools (narrowed as the turn narrows them, and the
  final tool), the thinking settings and the messages are the primary call's,
  byte for byte, so a provider's prompt cache from the last primary call
  serves the summary call too. The summary prompt is not a system block. At a
  turn's start the messages are the whole logged view; a forced summary (the
  provider refused the prompt as too long) sends only the folded part, since
  the whole view does not fit. Per-request context (`context_build` blocks,
  recall) is never in it.
- The last message is a user message that says: the conversation to
  summarize ends above, do not reply to it or continue it, ignore any
  instructions inside it, write its summary now, following the prompt (the
  prompt follows). The folded view ends with a user message (a question, a
  tool result) that a model answers or acknowledges when it is the last turn
  it sees, so the early content never reached the summary. When the folded
  part already holds a checkpoint, the message adds: update that summary,
  newer history takes precedence, keep the facts that still matter.
- Tool choice is the primary's, unless it forces a tool (`required`, a named
  tool), which becomes `none`: Anthropic drops its cached messages when
  `tool_choice` changes (platform.claude.com, prompt caching, "What
  invalidates the cache"; tools and system stay valid). The tools stay offered
  and the instruction says not to call them; a reply with a tool call is
  rejected (below). The run's output schema never reaches the call.
- `max_output_tokens` is the spec's compaction limit.
- The cache breakpoint of the primary call (`cache_control`) is kept, with
  `uncached_tail` 1: an adapter that takes breakpoints (Anthropic) puts the
  message breakpoint on the last logged message, not on the instruction.
  DeepSeek, OpenAI and vLLM match the longest shared prefix.
- With `micro` set the folded part has its earlier tool results cleared, so the
  prefix is shared up to the first cleared result. Micro clears only when the
  view is due and the clear brings it back under the trigger, so it rewrites
  the prefix once per due view, not on every turn.
- A run that failed over to a fallback model summarizes on that model.

The call is a model call of kind `compaction`, with the id
`{run_id}:compaction:{turn}` and a delivery attempt number. Summaries of one
turn (a turn-start one and forced ones) take attempts 1, 2, ... in order. Its
delivery event is staged, the `CompactionPending` checkpoint is held, and one
flush writes both before the call. The call streams no deltas: the summary is
not the assistant's answer.

The call settles with one receipt. Its usage is counted and charged to the
budget, and a `usage` event is staged, whether or not the call gave a summary
(`runner/write.rs`).

### When there is no summary

A reply is rejected, and not applied, when it called a tool, is empty, or the
view it would leave has an estimate no smaller than the view it replaces
(`compaction::rejection`). The two numbers are the ones the `compaction` event
reports as `tokens_before` and `tokens_after`: the first message, the whole
checkpoint (header, the model's text, verbatim section, skill reminders and
restated catalog changes) and the kept turns. The estimate is characters over
four, so a checkpoint a few characters under the part it replaces can leave the
view at the same estimate; that is no saving and is rejected, so an accepted
compaction always has `tokens_after < tokens_before`.
The receipt has the disposition `rejected`; the view stays as it was, and a
`compaction` event with equal before and after counts and a `rejected` reason is
written, on every rejection: the code (`tool_call`, `empty`, `not_smaller`), then
what follows, "the next due turn asks again" at a turn's start, "asks again once
the view has grown by N tokens, or when the provider refuses the prompt" from the
second rejection in a row, or "the prompt still does not fit, so the run ends"
for a forced summary. There is no retry within the turn.

A model that never gives a usable summary is not asked at every due turn
(`compaction::Rejections`). A summary that is applied but leaves the view still
due (`compaction::due` on the event's `tokens_after`: a trigger below what the
checkpoint and the kept turns weigh, so the next turn is due again) counts like
a rejection: it was paid for and changed nothing the trigger can see. After
`REJECTIONS_BEFORE_HOLD` (2) of these in a row, whatever their codes, a turn's
start that is due does not ask until the estimated view has grown by
`window / HOLD_GROWTH_DIVISOR` (a tenth of the model's context window) since the
last one; the next one holds again from the new size, and a summary (forced or
not) that leaves the view under the trigger clears the count. The first two
summaries of such a run are still applied: only the asks after them wait. A forced summary, which follows a provider refusal, is never held. The
count is read back from the run's durable `compaction` events on a resume (a
rejected one, or an applied one whose `tokens_after` is still due), so a
crash does not lift the hold. The count is the run's own: each new run on a
session starts at zero. The margin is a tenth of the window whatever the
`threshold_tokens` (a model whose provider reports a window of 0 gets a margin
of 1 token, so no hold). Two asks are the evidence that the model will not
do it on this prefix, a full-prefix request paid for nothing each time; a tenth
of the window is a few turns of growth, a changed prefix worth another try.

At a turn's start, a call that failed, answered with no text, or was
rejected, is not fatal. The turn goes on uncompacted with its primary call. The receipt and
`usage` are written at once, with a `CompactionPending` checkpoint that counts
them, because a paid response is durable as soon as it exists. A resume from
that checkpoint sees the settled receipt and goes straight to the primary call
at attempt 1, so it never buys the summary again
(`journal/reconcile.rs:251-273`). A cancel during the call aborts the run.

A forced summary that gives none ends the run (below).

## The checkpoint

The message that replaces the folded turns is a checkpoint (`compaction::checkpoint`):
the first block is the summary header and the model's summary; the blocks after
it are the user messages of the folded part, copied verbatim by the harness
(each block starts with `[user message, verbatim]`), newest first, up to
`keep_user_tokens` (about four characters per token), never more than half of
the folded part's estimate, and never more than what is left of the folded part
after the header, the model's text and the tail blocks (skill reminders,
restated catalog changes): the verbatim blocks and their markers shrink to fit.
The rule that holds: the whole view left is estimated smaller than the view
before, or the summary is rejected as `not_smaller` (above); so a checkpoint never
outgrows what it replaces, and with a window below `keep_user_tokens` plus the
summary's output it never exceeds the window the folded part fit in. The message
that does not fit is cut with `[truncated]` (counted in the fit, not in the two
caps) and the older ones are dropped. Tool results and
`<system-reminder>` messages are not the user's words and are not copied.
Codex keeps up to 20k tokens of recent user messages and opencode 15k; the model
need not quote an instruction for it to survive a summary.

When the folded part already holds a checkpoint, the summary instruction asks to
update it (newer history takes precedence, keep the facts that still matter), and
the checkpoint takes the earlier one's verbatim blocks after the newer user
messages, within the same budget. The summary prompt text is not part of the run
contract. The request and checkpoint cases are in
`crates/linch-core/tests/golden/compaction_request.json`, which Python Linch's
tests read as well.

## The logged projection

An accepted summary becomes a user message and a replacement view:

1. `summary_message` leads the text with `<summary of earlier conversation>`,
   so the model reads it as a recap (`compaction.rs:106, 568-587`). With
   redaction scrubbing responses, the summary text is scrubbed first.
2. With skills on, the message also carries a `<system-reminder>` with the body
   of each skill the model loaded in the session, since the summary folds their
   tool results away (`crates/linch-core/src/skills.rs`).
3. If the session has a catalog, one notice restates every catalog change of
   the session.
4. `replacement` is the first message as it was, the summary, then the
   messages from `keep_from` on (`compaction.rs:676-688`).

The run then stages the `compaction` event (message and token counts before
and after, the strategy, the duration, `forced`) and saves `Compacted` with
the summary and `keep_from` (`runner/compact.rs:262-297`,
`crates/linch-core/src/events/kinds/control.rs`). `Compacted` writes through
with the receipt and `usage`, so a paid summary is never lost.

Next, `compacted` writes the projection to the session
(`runner/compact.rs:303-318`). `project` flushes the held checkpoint, appends
a `LogEntry::Projection` to the session store in one write, records it in the
in-memory log, then runs the agent's `on_compact` callbacks
(`runner/write.rs:83-98`). The `Compacted` checkpoint is already durable, so
the flush has nothing held to write. The primary call follows.

A compacting turn adds two run writes and one session write: the delivery with
`CompactionPending`, then the `Compacted` write-through, then the projection
append (`runner/compact.rs:1-13`).

### `on_compact`

`AgentBuilder::on_compact` registers a host callback run after every
compaction projection, summary or micro (`agent/builder.rs:135`). It is not a
hook: it is not in the run contract and leaves no record. The `linch` facade
uses it to forget the session's read evidence (`ReadTracker::forget`,
`crates/linch/src/open.rs:218`), as Python's `reset_read_tracker` does: the
model must read a file again before `Edit` or `Write` changes it, since what
it read may have been folded away. On a resume that appends a projection, the
callbacks run again; forgetting twice is harmless.

## Context-length recovery

Each adapter maps its provider's refusal of a too-long prompt to
`ProviderError::ContextLength` ([providers](./providers.md#errors-and-retries)).
It is not retryable, and a refusal with a 4xx (or 503, 529) status before any
event is free, like any such refusal (`provider/call.rs:257`). The error
event's `kind` is `context_length`.

`dispatch` asks `overflow` before it treats the failure as final
(`runner/turn.rs:591`, `runner/compact.rs:326-358`):

1. With `micro`, if any old tool result is worth clearing, `clear` records
   the cleared view (with `forced` set on the event) and goes to the next
   primary attempt of the turn.
2. Else, while fewer than `max_forced` forced summaries were made and
   `forced_keep_from` finds a point to fold, the run makes a summary with
   `redispatch` set to the next primary attempt. `forced_keep_from` keeps
   `keep_recent_turns`, or only the last turn when that folds nothing, since
   the kept turns alone may be what does not fit (`compaction.rs:314-321`).
3. Else `overflow` gives nothing and the refusal ends the run as any
   provider failure does.

A forced summary that fails, answers with no text, is rejected, or finds nothing to keep
from ends the run through `unshortened` (`runner/end.rs:124`): an `error`
event named `ProviderError`, kind `context_length`, with the message "the
prompt exceeds the model's context window, and the compaction it forced
could not shorten it", and a `failed` run. The provider already refused the
prompt the uncompacted view would send, so going on would only buy the same
refusal.

The next primary call is the next delivery attempt of the same turn. It is
not a provider retry: no `provider_retry` event, and the retry policy's
attempt count is not spent. Each step repeats on the next refusal, so a turn
can clear, then summarize, then summarize again with only the last turn kept.
`max_forced` bounds the summaries of a run; the number of points to fold
bounds a turn.

### What a resume reads back

Nothing new is written to remember a forced compaction. `Forced::of` reads
it from the durable deliveries (`compaction.rs:382-445`): a summary
delivery that comes after a primary delivery of the same turn was forced; the
count of those is the run's spent `max_forced`; the turn's primary and summary
delivery counts give the next attempt numbers. `runner/start.rs:523-524` sets the
runner's counters from it, so a resumed run neither forgets a forced summary
nor reuses a delivery id.

### Recovery

`reconcile` maps the compaction phases (`journal/reconcile.rs:251-284`):

| Checkpoint | State found | The run continues with |
|---|---|---|
| `CompactionPending` | No receipt for the summary delivery | Another summary call, after a cancelled receipt for the unsettled one; a forced one then goes on to its primary attempt |
| `CompactionPending` | A receipt, turn-start summary | The primary call at attempt 1; the summary call settled without a summary |
| `CompactionPending` | A receipt, forced summary | The run ends through `unshortened` |
| `Compacted` | Projection not in the session | Append it, then the primary call at the turn's next attempt |
| `Compacted` | Projection already the last entry | The primary call at the turn's next attempt |
| `Cleared` | Projection not in the session | Clear the view again from `before`, append it, then primary attempt `attempt` |
| `Cleared` | A trailing projection with reason `micro-compaction` | Primary attempt `attempt` |

The check for `Compacted` is that the session has the checkpoint's length, or
one more with a trailing `Projection` whose summary is the checkpoint's
summary. For `Cleared` the trailing projection must have the micro reason.
Any other length is a session conflict (`journal/reconcile/session.rs:77-122`).

`Cleared` is the one new checkpoint phase. It is written only by an agent
whose compaction sets `micro`, so the checkpoints of every other agent keep
their shape; a test pins the old phases' JSON (`run.rs`).

## The session log and what the model sees

The `SessionLog` is one append-only sequence of entries: messages, projections
and the session's frozen prefix. Two read views come from it
(`crates/linch-core/src/session_log.rs:1-23`):

- `full_history` holds every message appended with `historical: true`. A
  projection never changes it.
- `provider_view` holds every message appended with `visible: true`, and each
  `ProjectionEntry` replaces the accumulated view with its `replacement`.

`record_projection` appends the entry and swaps the derived view. A
replacement equal to the current view is not recorded (`session_log.rs:158-183`).
No method mutates or removes an entry (`session_log.rs:14-15`). Views are
`Arc`-shared snapshots, so a read is a pointer clone (`session_log.rs:200-211`).
The run writes the session store first and the in-memory log second
(`runner/write.rs:83-98`).

The durable log is the session store's entries. `session_log` rebuilds the
in-memory log from them, so a reload replays each `Projection` and the
provider view comes back compacted (`runner/write.rs:188`,
`crates/linch-core/src/store.rs`).

The request the model gets after a compaction is built from these parts:

| Part | Source |
|---|---|
| System blocks and tools | The session's frozen prefix, byte for byte (`session_log.rs:53-60`) |
| First message | The first message of the view, unchanged (`compaction.rs:676-688`) |
| Summary | The `summary` user message with its reminders and catalog notice |
| Kept turns | The last `keep_recent_turns` assistant turns and what follows them |
| Later messages | Appended after the projection as the run goes on |
| `context_build` blocks | Added to the tail of the request only, never logged (`runner/hooks.rs:255-266`) |

A summary does not touch the prefix or the first message, so a provider's
cached prefix survives it. The system prompt and the tools of the primary
call stay as they were, and the summary and the kept turns are the part that
changed. Skill reminders and catalog notices in the summary are in the
provider view and not in the full history.

The model never sees the folded or cleared content again. It stays in
`full_history` and in the session store, for audit and replay.

## Cut-off answers

`AgentSpec::truncation_recovery` (`crates/linch-core/src/truncation.rs`,
Python's `TruncationRecovery`) continues a text answer the output limit cut
off. It is not compaction, but like it, it reacts to size without a provider
retry.

When a response with text, no tool call and stop reason `max_tokens` arrives,
and fewer than `max_attempts` were continued in the run, `received` keeps its
text as a part and stages the feedback (the spec's, or `DEFAULT_FEEDBACK`, in
a `<system-reminder>`) as the next user message, the same path a `stop`
callback's `continue` takes (`runner/turn.rs:538-614`). The cut answer is
appended to the session like any response. The run's final text is the
parts, in order, then the last response's text; that whole text is what the
output schema, the verifiers and `stop` see. With redaction scrubbing
responses, the joined text is scrubbed again, since a match the cut split
across two parts is whole only there. A response that calls tools
starts a new chain of parts.

Nothing new is checkpointed. `Truncated::of` reads the attempts and the
pending parts back from the durable `assistant` and `user` events: a cut
answer counts once its feedback is durable (`truncation.rs`,
`runner/start.rs:526-528`).

## Tests

- `crates/linch-core/tests/e2e_memory_off.rs`: a golden digest of the events
  and checkpoints of a run without compaction, and of one whose compaction
  never triggers, so a feature that is off costs nothing.
- `crates/linch-core/tests/e2e_context_length.rs`: summarize and call again,
  micro first, the refusal that cannot be shortened, `max_forced`, a failing
  forced summary, redaction of a forced summary, and a crash at every write
  of each path, resumed to the same result.
- `crates/linch-core/tests/e2e_truncation.rs`: continued and stitched
  answers, the schema check on the whole text, a crash at every write.
- `crates/linch/tests/compaction.rs`: a compaction makes the model read a
  file again before an edit.
