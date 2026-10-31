# Agent & session

[← Usage guide](./README.md)

The `Agent` holds long-lived configuration; the `Session` holds the state of one
conversation. This page covers the configuration knobs that shape an agent's
lifecycle: where conversations are stored, which subsystems are active, the
system prompt, how context is kept under the window (compaction), and how
spending is capped (budgets).

- Model and provider selection live in [providers.md](./providers.md).
- Tools, timeouts, retry, the Bash backend, dependencies, and permissions live
  in [tools.md](./tools.md).
- Cross-cutting extension (middleware, RAG, verifiers) is unified
  under [hooks.md](./hooks.md).

## Tool and response reliability

`Agent` uses `ReliabilityPolicy()` by default for every provider. An empty
`end_turn`, a whole-text `<tool_call>...</tool_call>` envelope, or malformed
native tool arguments receives a logged Linch diagnostic asking the model to
resend. No tool in a rejected native batch runs. The default allows two
correction turns across the entire run; exhaustion ends with an error result.
Transport and structural stream errors retain their provider-error behavior.
Ordinary answers, refusals and truncated output keep their existing behavior.
Text envelopes are never executed; prose and fenced examples remain plain text.

Declare tool ordering and completion requirements when your application needs
them:

```python
from linch import Agent, CompletionGate, ReliabilityPolicy, ToolPrerequisite

agent = Agent(
    model="gpt-5",
    tools=[lookup_customer, update_customer],
    reliability=ReliabilityPolicy(
        prerequisites=(ToolPrerequisite("update_customer", ("lookup_customer",)),),
        completion=CompletionGate(("update_customer",)),
    ),
)
```

Use the tools' registered canonical names. Requirements are validated after
tools load, before the first provider call. A prerequisite must have succeeded
in an earlier batch of the same run. Calls in one batch cannot unlock each
other. Only successful foreground results after hooks count; errors, denials,
interruptions and background acknowledgements do not count. A final-answer tool
cannot serve as completion evidence. The completion gate checks every final
route, including a loop-guard forced answer, and shares the correction budget.
Explicit host early-success stops through `StopPredicateHook` take precedence.

Set `retry_invalid_responses=False` to disable response corrections while
retaining configured prerequisites and completion requirements. Set
`max_correction_turns=0` to fail immediately when a correction is needed, or
`reliability=None` to disable the subsystem entirely. A clean run adds no
reliability events, feedback or provider calls. Intervention emits
`ReliabilityEvent`; correction counts and referenced successful tools survive
resume, and changing the policy rejects resume. State belongs to each run and
is not inherited by subagents.

---

## Agent vs Session lifecycle

Create the `Agent` once and reuse it. Open a fresh `Session` per conversation:

```python
agent = Agent(model="gpt-5", session_store=InMemorySessionStore())

# one conversation
session = await agent.session()                  # new id
async for event in session.run("first turn"):
    ...
async for event in session.run("second turn"):   # same session — full history
    ...

# a different conversation, same agent
other = await agent.session(id="user-42")        # resume/attach by id
```

`session.run(prompt)` appends the user message, drives the
[turn loop](../architecture.md), and yields [events](./events.md). Call it again
on the same `Session` to continue the thread — history persists in the session
store. When you are done, `await agent.close()` cancels any live background
workers, flushes stores, and closes hooks that expose `close`/`aclose`.

Each Agent also owns one per-agent `Context` scope. It contains the agent's
service registrations, event bus, tool pipeline, and effective execution world;
it is not process-global state. Pass `context=` to supply a parent scope; the
Agent creates and owns a child scope, leaving the supplied context alive. If a
host needs to retain a parent scope, pass a child from `parent.scope(...)`.
Closing the agent disposes its child scope after quiescing active work. Disposal
is retryable: if a custom
teardown fails, a later `await agent.close()` retries only unfinished cleanup.

### Optional plugins

Plugins are mounted asynchronously and can be dormant while their required
services are absent. Setup is synchronous and returns a `Disposable`; removal
awaits cleanup and a later service registration eventually activates a fresh
scope (registration emits synchronously, reconciliation is async):

```python
from linch import Agent, AskUserPlugin, MailboxPlugin

agent = Agent(model="gpt-5")
mount = await agent.use(AskUserPlugin(), ask_user_handler)
await mount.dispose()  # idempotently removes the tool
```

Mailbox coordination uses the same API:
`await agent.use(MailboxPlugin(), mailbox)`. Plugin-provided services
are published only after setup passes the `provides` audit, and failed cleanup
remains retryable through the returned handle or `agent.close()`.

### Releasing a single session

`agent.close()` tears down the whole agent. To release just one long-lived
conversation — free its in-memory registration and drain its owned background
work — without closing the agent or deleting durable history, use the
per-session lifecycle APIs:

```python
# Release by instance or id (idempotent; unknown/already-released is a no-op).
await agent.release_session(session)
await agent.release_session("user-42")

# Or from the session itself.
await session.aclose()

# Session is also an async context manager (aclose(force=True) on exit).
async with await agent.session(id="user-42") as session:
    async for event in session.run("hello"):
        ...
```

Releasing a session with an **active run** raises unless you pass `force=True`.
A forced release aborts the run, drains owned background work so finalizers run,
and recursively releases retained child sessions. Durable history in the session
store is always preserved — a released session can be reattached later by id.

### Bounding cached idle sessions

Long-lived hosts can opt into automatic release of idle conversations:

```python
agent = Agent(model="gpt-5", session_store=store, max_idle_sessions=100)
session = await agent.session(id="user-42")
async for event in session.run("hello"):
    ...
```

`max_idle_sessions=None` (the default) keeps all live sessions until explicitly
released. A non-negative integer bounds cached idle sessions; `0` releases each
session once its run iterator finishes. Run completion and reattachment by id
refresh recency. When the bound is exceeded, Linch releases the least recently
used idle sessions through `release_session()`, preserving stored history.
Opening a session does not itself trigger eviction; the bound is checked after
runs and protected background work finish.

Active runs, detached tool tasks, background workers, and undelivered in-process
notifications or child events are protected and do not count toward the idle
bound. A parent with a busy retained child is protected too. Completion of
protected background work reapplies the bound; queued delivery stays live until
the next run drains it. Idle retained children can be released independently or
along with their parent. Worker continuation needs a
live retained child, so choose a bound that fits your worker lifetime. Reattach
an evicted conversation with `await agent.session(id=...)`; a previously released
`Session` instance cannot run again.

### Steering an in-flight run

`session.align(prompt, images=..., timeout_s=...)` injects a user message into a
**running** session without aborting it. The message is queued and drained at
the next turn boundary — after the current turn's tool results, before the next
provider call — emitting a `UserEvent(subtype="alignment")` and resolving the
awaited `align()` call once injected. Calling `align()` on an idle session
raises `ConfigError`.

```python
run_task = asyncio.create_task(consume(session.run("start the migration")))
# ... user types while the agent works:
await session.align("skip the staging environment, go straight to prod checks")
```

**Durability.** With a `RunStore` configured, undrained alignment entries are
snapshotted into every run checkpoint (`RunCheckpoint.pending_alignment`) and
restored on `resume()`, so steering intent survives a crash. Delivery is
in-order and **at-least-once**: an entry enqueued after the last checkpoint save
is lost with the crash, and an entry drained just before a crash may be injected
once more on resume. A resume that lands *mid-turn* (pending tool batch) defers
the drain past the re-executed tools so the injected message never breaks the
assistant→tool-results provider order; a resumed turn that finalizes without
another provider call drops restored entries silently.

---

## Session store

The session store persists the message snapshots needed by the session's single
append-only `SessionLog`: `provider_view` (model-visible) and `full_history`
(audit). Both are deep-copied, read-only projections. Grow history with
`await session.append(...)` — it writes to the session store *and* the log,
so the messages survive a reload. `session.session_log.append(...)` updates
only the in-memory log and is not a durable session mutation. Record
compaction with `session.session_log.record_projection(...)`. Per-request context-builder output
is ephemeral and is never part of the log. Pick ephemeral for stateless workers,
Postgres to survive restarts.

Because each read of `provider_view`/`full_history` builds a detached deep copy,
reach for the cheap accessors when you only need a count or the newest turn —
`session.message_count`, `session.last_provider_message()`, or on the log itself
`visible_count` / `history_count` / `last_visible()`. On a long session that is
the difference between copying the whole conversation and copying nothing.

`MessageEntry` and `ProjectionEntry` are in-memory log entries. The durable
boundary is the session snapshot, not a persisted projection-entry journal. On
load, Linch seeds a fresh log from the stored historical and visible snapshots.

```python
import os
from linch import InMemorySessionStore, PostgresSessionStore

# Ephemeral (tests, single-request workers)
store = InMemorySessionStore()

# Persistent (keep history across restarts)
store = PostgresSessionStore(os.environ["LINCH_POSTGRES_DSN"])

agent = Agent(model="gpt-5", session_store=store)
```

`SessionStore` is a protocol, so a host app can implement its own backend
(Redis, Postgres, …) by satisfying the same interface. For durable *run*
checkpoints (resume across process restarts, durable HITL approvals) also pass a
`run_store=PostgresRunStore(dsn)` — see [workflows.md](./workflows.md) and
[deep-agent.md](./deep-agent.md).

### Incremental durability

`DurabilityOptions` enables durability without changing the default 2.x path:

```python
import os
from linch import Agent, DurabilityOptions, PostgresRunStore, PostgresSessionStore

dsn = os.environ["LINCH_POSTGRES_DSN"]

agent = Agent(
    model="gpt-5",
    session_store=PostgresSessionStore(dsn),
    run_store=PostgresRunStore(dsn),
    durability=DurabilityOptions.strict_v1(),
)

await session.notify(message, delivery_id="webhook:123")
```

`durable_inbox` gives exactly-once insertion into session history by delivery
ID (not exactly-once external effects). `exact_model_input` freezes every
pending provider request and replays it exactly after a crash. Enabled
capabilities fail closed when a custom store does not implement the required
optional protocol. In-memory stores provide the same process semantics but do
not survive a restart. Snapshots can contain prompts, context, and tool schemas;
the SDK verifies integrity but encryption and access control belong to the host.

---

## Feature flags

`FeatureFlags` turns off subsystems you don't use so they never connect during
`session()`. This trims startup work and the system prompt.

In Linch 2.0 the SDK-safe defaults are all `False`: a bare `Agent` does not
discover project skills/subagents, connect to ambient MCP, or enable a virtual
filesystem. This is intentional resource trust isolation, not a coding-agent
configuration. Opt in only for a trusted project or use an explicit preset:

```python
from linch import Agent, FeatureFlags, workspace_tools

agent = Agent(
    ...,
    tools=workspace_tools(),
    features=FeatureFlags(
        skills=True,
        subagents=True,
        filesystem=True,
        mcp=False,  # turn on only with configured, trusted servers
    ),
)
```

```python
from linch.config import FeatureFlags

agent = Agent(
    ...,
    features=FeatureFlags(skills=False, subagents=False, mcp=False),
    # also: filesystem=False to disable the virtual filesystem subsystem
)
```

Each flag gates one subsystem: `skills` (the `Skill` tool + skill loading),
`subagents` (the `Subagent`/`SubagentContinue` tools + registry), `mcp` (MCP
server connections), and `filesystem` (the virtual filesystem and
large-result offloading from [filesystem.md](./filesystem.md)). `filesystem=False`
disables offloading even if you pass a backend.

---

## System prompt control

By default Linch ships a domain-neutral identity prompt. It mentions only the
tools actually registered on the agent. You can append to it, replace it
wholesale, or compose reusable sections. Coding doctrine belongs in an
explicit workspace preset or application-owned prompt.

```python
from linch.config import SystemPromptConfig, SystemPromptSection

# 1. Append instructions to the built-in Linch prompt
agent = Agent(..., system_prompt="Always reply in formal English.")

# 2. Replace the entire SWE identity with your own
agent = Agent(
    ...,
    system_prompt_config=SystemPromptConfig(
        replace_defaults=True,
        append="You are a financial analyst. Only discuss stocks and bonds.",
    ),
)

# 3. Add reusable prompt sections without replacing the defaults
agent = Agent(
    ...,
    system_prompt_config=SystemPromptConfig(
        sections=[
            SystemPromptSection(
                name="domain-policy",
                text="When handling invoices, preserve source document IDs in every answer.",
                placement="after_defaults",
            )
        ]
    ),
)
```

Use the plain `system_prompt=` string for the common "add a line or two" case.
Reach for `SystemPromptConfig` when you need to drop the SWE identity entirely
(`replace_defaults=True`) or attach named, placement-controlled sections that
compose cleanly with subsystem prompt blocks (filesystem, skills, …).

---

## Compaction

When the projected context approaches the provider's window, the compaction
strategy summarizes old messages to free space. It is transparent to the
caller; a `CompactionEvent` is emitted when it runs.

```python
from linch import Agent, DetailedCompaction

# DefaultCompaction is the default. DetailedCompaction is opt-in and uses a
# continuation-safe summary structure for long-running sessions.
agent = Agent(model="gpt-5", compaction=DetailedCompaction())
```

### Accounting for compaction

In a normal run, the configured strategy's provider delivery is an accounted
auxiliary call. Its valid `Usage` is added to the same cumulative total as the
primary turn, and its receipt settles before the summary is applied or another
provider call starts. A summary that reaches the token or USD limit therefore
stops the run before the main turn is dispatched. An unusable summary (empty,
non-text, tool-using, or oversized) is rejected but still charged when valid
final usage was observed.

`DefaultCompaction` and `DetailedCompaction` have a deterministic paid-result
replay seam for durable runs. Linch stores a bounded accepted summary, its
receipt identity, and an ordered hash-only source recipe; after a crash, a
matching durable source can apply that summary without another provider call or
duplicate charge. Required invoked-skill bodies are flushed through the
existing session store before that recipe is written. A changed source,
strategy configuration, listing wrapper, or run contract fails closed rather
than applying a stale summary.

The public `CompactionStrategy` protocol remains unchanged. A custom strategy
receives an accounted provider facade for calls made through Linch, including
the run's cancellation and provider-slot/limiter guarantees. Opaque network
calls outside that facade are not observable, and arbitrary multi-call or
transform logic has no automatic deterministic paid-replay guarantee. The
standalone `summarize_with_provider(...)` helper remains available with its
existing direct-provider behavior; it is not a durable run receipt by itself.

### Compaction ladder

`CompactionLadder` adds cheap, LLM-free recovery rungs before and around
summarization. It is opt-in — the default (`compaction_ladder=None`) keeps one
compaction retry per turn. Both policies use the same recovery driver.

```python
from linch import Agent, CompactionLadder

agent = Agent(
    model="gpt-5",
    compaction_ladder=CompactionLadder(
        micro=True,                 # rung 1: LLM-free tool-result elision
        keep_recent_turns=10,       # never elide results in the last N turns
        max_forced_compactions=3,   # per-run circuit breaker on LLM compaction
    ),
)
```

- **Micro-compact (rung 1)** — old `ToolResultBlock` contents are replaced with
  a short placeholder via copy-on-write: no LLM call, `tool_use_id` pairing
  preserved, `full_history` untouched. Runs proactively when the projection
  crosses ~80% of the window, and reactively (once per turn) when the provider
  raises `ContextLengthError`. Emits `CompactionEvent(strategy="micro")`.
- **Forced compaction (rung 2)** — the configured strategy summarizes as usual,
  but capped at `max_forced_compactions` per run; after that the
  `ContextLengthError` surfaces instead of looping forever.
- The proactive "did we free enough?" check uses the agent's `token_estimator`.
  The default heuristic counts only text blocks, so pass an estimator that also
  counts tool-result content to get the most out of micro-compaction. The
  reactive rung shrinks the real payload, so it helps regardless.

---

## Truncation recovery

When a model's text response is cut off because it hit the output-token limit
(normalized to `stop_reason == "max_tokens"`), the default loop returns that
truncated text as the final answer. Linch never silently raises the output cap —
that changes cost and latency, which is your policy decision.

`TruncationRecovery` is the explicit, opt-in knob: a truncated text turn is not
finalized while attempts remain — the loop injects a continuation nudge as a user
turn and runs again so the model can finish. With `truncation_recovery=None`
(the default) behavior is byte-identical.

```python
from linch import Agent, TruncationRecovery

agent = Agent(
    model="gpt-5",
    truncation_recovery=TruncationRecovery(
        max_attempts=2,          # continuation turns to spend per run
        # feedback="Continue ...",  # the nudge sent to the model (has a default)
    ),
)
```

Once `max_attempts` is exhausted the truncated answer is returned as-is, with
`ResultEvent.stop_reason == "max_tokens"` so the host can still tell it was cut
off. Each continuation is a normal turn, so it counts against `max_turns` and the
run `RunBudget`.

---

## Run budgets

`RunBudget` caps total spending — tokens, USD, or both — for a run **and every
subagent it spawns**. Child sessions inherit the parent's budget object by
reference, so one cap covers the whole agent tree.

```python
from linch import Agent, RunBudget
from linch.session import RunOptions

budget = RunBudget(max_tokens=500_000)          # and/or max_cost_usd=2.0

session = await agent.session()
async for event in session.run("do the task", RunOptions(budget=budget)):
    if event.type == "budget" and event.kind == "warning":
        print(f"90% spent: {event.spent_tokens} tokens")
    elif event.type == "budget" and event.kind == "exceeded":
        print("budget exhausted — run stops with an error result")

print(budget.spent_tokens, budget.remaining_tokens, budget.exceeded)
```

Behavior:

- The loop settles each physical provider delivery before it continues. This
  includes the auxiliary compaction delivery, so its usage is charged before
  the summary is applied and admission is checked again before the next
  primary turn. When exceeded it emits `BudgetEvent(kind="exceeded")`, an
  `ErrorEvent` named `BudgetExceededError`, and a `ResultEvent(subtype="error")`
  — the session history stays intact and the session remains usable.
- `BudgetEvent(kind="warning")` fires once per budget object when spending first
  reaches `warn_ratio` (default 0.9) of any limit. Shared budgets warn once
  across the whole tree, not once per run.
- Precedence: `RunOptions(budget=...)` > a budget inherited from a parent
  session (subagents) > `Agent(budget=...)`.
- Token counts sum all `Usage` buckets from primary and auxiliary deliveries.
  USD costs come from `linch.pricing`; unknown models do not receive a
  synthetic price, so their known tokens remain in the total while cost is
  partial and only a token limit can bind for that delivery.
- If a dispatched delivery is cancelled or fails before valid final usage, its
  usage is `unknown`, never zero. A capped run fails closed before another
  provider call until the host resolves that accounting fence; an uncapped run
  may finish with a lower-bound total and `ResultEvent.usage_complete=False`.
- A live `RunBudget` object carries its receipt owner and charged delivery IDs
  across the in-process agent/subagent tree. Resume may adopt a checkpoint only
  with a fresh zero-spent budget; a fresh nonzero or owner-mismatched capped
  budget fails closed rather than silently replaying charges.

These ownership fences are intentionally in-process. They do not promise
distributed-tree recovery, exactly-once external-provider billing, or a
reservation against concurrently admitted deliveries.

Verification retries (schema repair, [verifier hooks](./hooks.md)) and workflow
subagents all draw from the same budget, so a strict gate can never loop
unboundedly.

---

## Provider concurrency

`RunBudget` caps how much a run may spend; `Agent(max_provider_concurrency=N)`
caps how many live provider calls that agent may have in flight at once.

```python
agent = Agent(model="gpt-5.5", provider=provider, max_provider_concurrency=8)
```

This bounds **provider calls, not `run()` calls** — the distinction matters
because one `run()` fans out into many calls: every turn of the loop, every
same-model retry, every model-fallback swap, every compaction summarization, and
every subagent it spawns. A semaphore your host wraps around `agent.run()` caps
none of those; this does.

The cap is per-`Agent`, so two agents in one process keep separate budgets. The
gate is held around the provider call itself and released across retry backoff,
so a call waiting to retry re-queues instead of sitting on a slot. When unset
there is no gate at all and no added cost.

For a shared budget across several agents, per-model quotas, or a token bucket,
pass your own [`Limiter`](./extending.md#limiter--gate-every-live-provider-call)
instead. Passing both `limiter=` and `max_provider_concurrency=` raises
`ConfigError`.

The tool side has its own separate cap, `Agent(max_tool_concurrency=...)` — see
[Tools](./tools.md#the-scheduler).

---

## Related pages

- [Providers](./providers.md) — model selection and capabilities.
- [Hooks](./hooks.md) — middleware, RAG, verifiers, stop predicates, and OTel setup.
- [Workflows](./workflows.md) / [Deep agent](./deep-agent.md) — durable runs and
  the subagent tree that budgets cover.
