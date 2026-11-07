# Compaction

> Part of the [linch-rust architecture guide](./README.md).

Compaction folds the older part of a long provider view into one summary. A
run does it at the start of a turn, before the primary provider call, when
the agent's spec turns it on. The summary comes from an auxiliary model call
with its own delivery and receipt. The result is a logged projection on the
session's append-only log: the model sees a shorter view, and the full
history keeps every message. The pure parts are in
`crates/linch-core/src/compaction.rs`; the run's part, with its barriers, is
`crates/linch-core/src/runner/compact.rs`. Use this page when you change when
a run compacts, what the summary holds, or how a run recovers a paid summary.

Python Linch has the same shape ([compaction](../../../docs/architecture/compaction.md)).
What differs in Rust:

- There is one strategy, named `summary` (`compaction.rs:33-34`). There is no
  compaction ladder, no LLM-free `micro_compact` rung, no `DetailedCompaction`
  and no custom strategy. `CompactionSpec` is data, and the prompt is its only
  hook (`compaction.rs:54-77`).
- There is no reactive path. Nothing in `linch-core` compacts in response to a
  context-length error; a run compacts only at a turn start.
- The size estimate is built in: about four characters per token and 1000 per
  image (`compaction.rs:117-146`). There is no pluggable token estimator.
- A paid summary is kept whole in the `Compacted` checkpoint, not as a
  hash-only recipe. A resume appends it without another provider call
  (`crates/linch-core/src/run.rs:126-134`, `runner/compact.rs:155-171`).
- The projection is always written to the session store as a log entry
  (`runner/write.rs:83-92`). There is no store detection and no opt-in.

## Turning it on

`AgentSpec::compaction` is `None` by default, which never compacts
(`crates/linch-core/src/agent/spec.rs:60-63`). Set a `CompactionSpec`
(`compaction.rs:54-77`):

| Field | Default | Meaning |
|---|---|---|
| `threshold_tokens` | none | Compact when the estimated view reaches this many tokens. Without it, compact near the provider's context window (below) |
| `keep_recent_turns` | 10 | Assistant turns kept verbatim after the summary; at least 1 |
| `prompt` | `DEFAULT_PROMPT` | Instructions for the summary call (`compaction.rs:22-28`) |
| `max_output_tokens` | 4096 | Most tokens the summary may use |

`CompactionSpec::new(n)` sets the threshold and leaves the rest at their
defaults (`compaction.rs:93-100`). `AgentBuilder::build` refuses a zero
threshold, a zero `keep_recent_turns` or a zero `max_output_tokens` with
`BuildError::Invalid` (`compaction.rs:102-114`, `agent/builder.rs:216-218`).

The spec is part of the run contract, so changing it makes agents with the old
and the new setting incompatible: a resume of an old run is refused
(`agent/spec.rs:131-155`, `runner/start.rs:425-431`).

## When a run compacts

A turn starts at `Step::Compact` when the spec has compaction, and at
`Step::Dispatch` otherwise (`runner/turn.rs:237-248`). `compact` returns to
the primary call without doing anything when any of these hold
(`runner/compact.rs:62-77`):

1. The run was cancelled: it aborts instead (`compact.rs:57-60`).
2. `max_turns` or the budget already allows no more calls. The primary call
   ends the run with its own terminal (`compact.rs:65-68`).
3. `keep_from` finds no point to fold. The view needs at least one assistant
   turn between its first message and the kept turns (`compaction.rs:172-187`).
   A one-prompt first turn never compacts.
4. The view is not due.

Due is decided by `due` (`compaction.rs:148-170`):

- With `threshold_tokens`, the estimate is at or above it.
- Without it, the estimate plus an output reserve reaches 80% of the model's
  context window. The reserve is the spec's `max_output_tokens` when set and
  not 0, else a fifth of the window, at least 1 and at most 32768. The window
  is `Provider::context_window` for the spec's model
  (`agent.rs:181-184`, [providers](./providers.md)).

The estimate counts text, thinking, catalog notices, tool inputs and tool
results; images cost a fixed amount; signatures and redacted reasoning are not
counted (`compaction.rs:117-146`).

`keep_from` is the `keep_recent_turns`-th assistant message from the end of
the view. The kept part starts at that message, so a tool result is never cut
off from its call (`compaction.rs:172-187`).

## The summary call

If the view is due, the run builds a summary request (`compaction.rs:189-207`,
`runner/compact.rs:78-91`):

- The messages are the view before `keep_from`.
- The system block is the summary prompt, not cacheable.
- The tools are the ones the session's catalog holds, the frozen prefix tools
  plus any added since, with tool choice disabled. A provider may refuse tool
  calls and results in a request that defines no tools (`compaction.rs:189-191`,
  `compact.rs:84-89`, `catalog.rs:120-142`).
- `max_output_tokens` is the spec's compaction limit. No cache control is set;
  only a primary call asks for prompt caching (`agent.rs:169-179`).

The call is a model call of kind `compaction`, with the id
`{run_id}:compaction:{turn}` and a delivery attempt number
(`provider/call.rs:96-108`). Its delivery event is staged, the
`CompactionPending` checkpoint is held, and one flush writes both before the
call (`compact.rs:90-96`). The call streams no deltas: the summary is not the
assistant's answer (`compact.rs:97-98`).

The call settles with one receipt. Its usage is counted and charged to the
budget, and a `usage` event is staged, whether or not the call gave a summary
(`compact.rs:99-105`, `runner/write.rs:121-144`).

### When there is no summary

A call that failed, or answered with no text, is not fatal. The turn goes on
uncompacted with its primary call (`compact.rs:114-120`). The receipt and
`usage` are written at once, with a `CompactionPending` checkpoint that counts
them, because a paid response is durable as soon as it exists. A resume from
that checkpoint sees the settled receipt and goes straight to the primary call
at attempt 1, so it never buys the summary again
(`journal/reconcile.rs:210-220`). A cancel during the call aborts the run
(`compact.rs:106-111`).

## The logged projection

An accepted summary becomes a user message and a replacement view:

1. `summary_message` leads the text with `<summary of earlier conversation>`,
   so the model reads it as a recap (`compaction.rs:36-37, 209-212`).
2. With skills on, the message also carries a `<system-reminder>` with the body
   of each skill the model loaded in the session, since the summary folds their
   tool results away (`compact.rs:122-126`, `crates/linch-core/src/skills.rs:171`).
3. If the session has a catalog, one notice restates every catalog change of
   the session (`compact.rs:127-132`).
4. `replacement` is the first message as it was, the summary, then the
   messages from `keep_from` on (`compaction.rs:214-226`).

The run then stages the `compaction` event (message and token counts before
and after, the strategy, the duration) and saves `Compacted` with the summary
and `keep_from` (`compact.rs:133-147`, `crates/linch-core/src/events/kinds/control.rs:64`).
`Compacted` writes through with the receipt and `usage`, so a paid summary is
never lost (`journal.rs:192-193`).

Next, `compacted` writes the projection to the session (`compact.rs:158-171`).
`project` flushes the held checkpoint, appends a `LogEntry::Projection` to the
session store in one write, then records it in the in-memory log
(`runner/write.rs:83-92`). The `Compacted` checkpoint is already durable, so
the flush has nothing held to write. The primary call follows.

A compacting turn adds two run writes and one session write: the delivery with
`CompactionPending`, then the `Compacted` write-through, then the projection
append (`compact.rs:3-10`).

### Recovery

`reconcile` maps the compaction phases (`journal/reconcile.rs:210-230`):

| Checkpoint | State found | The run continues with |
|---|---|---|
| `CompactionPending` | No receipt for the summary delivery | Another summary call, after a cancelled receipt for the unsettled one |
| `CompactionPending` | A receipt | The primary call; the summary call settled without a summary |
| `Compacted` | Projection not in the session | Append it, then the primary call |
| `Compacted` | Projection already the last entry | The primary call |

The check for `Compacted` is that the session has the checkpoint's length, or
one more with a trailing `Projection` whose summary is the checkpoint's
summary. Any other length is a session conflict
(`journal/reconcile/session.rs:75-98`).

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
(`runner/write.rs:89-90`).

The durable log is the session store's entries. `session_log` rebuilds the
in-memory log from them, so a reload replays each `Projection` and the
provider view comes back compacted (`runner/write.rs:181-196`,
`crates/linch-core/src/store.rs:197-219`).

The request the model gets after a compaction is built from these parts:

| Part | Source |
|---|---|
| System blocks and tools | The session's frozen prefix, byte for byte (`session_log.rs:53-60`) |
| First message | The first message of the view, unchanged (`compaction.rs:214-226`) |
| Summary | The `summary` user message with its reminders and catalog notice |
| Kept turns | The last `keep_recent_turns` assistant turns and what follows them |
| Later messages | Appended after the projection as the run goes on |
| `context_build` blocks | Added to the tail of the request only, never logged (`runner/hooks.rs:117-129`) |

Compaction does not touch the prefix or the first message, so a provider's
cached prefix survives it (`compaction.rs:7-10`). The system prompt and the
tools of the primary call stay as they were, and the summary and the kept
turns are the part that changed. Skill reminders and catalog notices in the
summary are in the provider view and not in the full history
(`compact.rs:12-18`).

The model never sees the folded messages again. They stay in `full_history`
and in the session store, for audit and replay.
