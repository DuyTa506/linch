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
- Cross-cutting extension (observers, middleware, RAG, verifiers) is unified
  under [hooks.md](./hooks.md).

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

---

## Session store

The session store persists conversation state (`provider_view`, `full_history`,
tasks). Pick ephemeral for stateless workers, SQLite to survive restarts.

```python
from linch.sessions import InMemorySessionStore, SqliteSessionStore
from pathlib import Path

# Ephemeral (tests, single-request workers)
store = InMemorySessionStore()

# Persistent (keep history across restarts)
store = SqliteSessionStore(Path("~/.myapp/sessions.db").expanduser())

agent = Agent(model="gpt-5", session_store=store)
```

`SessionStore` is a protocol, so a host app can implement its own backend
(Redis, Postgres, …) by satisfying the same interface. For durable *run*
checkpoints (resume across process restarts, durable HITL approvals) also pass a
`run_store=SqliteRunStore(...)` — see [workflows.md](./workflows.md) and
[deep-agent.md](./deep-agent.md).

---

## Feature flags

`FeatureFlags` turns off subsystems you don't use so they never connect during
`session()`. This trims startup work and the system prompt.

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

By default Linch ships a software-engineering identity prompt. You can append to
it, replace it wholesale, or compose reusable sections.

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

### Compaction ladder

`CompactionLadder` adds cheap, LLM-free recovery rungs before and around
summarization. It is opt-in — with the default (`compaction_ladder=None`)
behavior is byte-identical to the legacy single-retry path.

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

- The loop charges the budget after every provider turn and checks it before the
  next one. When exceeded it emits `BudgetEvent(kind="exceeded")`, an
  `ErrorEvent` named `BudgetExceededError`, and a `ResultEvent(subtype="error")`
  — the session history stays intact and the session remains usable.
- `BudgetEvent(kind="warning")` fires once per budget object when spending first
  reaches `warn_ratio` (default 0.9) of any limit. Shared budgets warn once
  across the whole tree, not once per run.
- Precedence: `RunOptions(budget=...)` > a budget inherited from a parent
  session (subagents) > `Agent(budget=...)`.
- Token counts sum all `Usage` buckets. USD costs come from `linch.pricing`;
  unknown models charge `$0`, so for unpriced models only the token limit binds.

Verification retries (schema repair, [verifier hooks](./hooks.md)) and workflow
subagents all draw from the same budget, so a strict gate can never loop
unboundedly.

---

## Related pages

- [Providers](./providers.md) — model selection and capabilities.
- [Hooks](./hooks.md) — observers, middleware, RAG, verifiers, stop predicates.
- [Workflows](./workflows.md) / [Deep agent](./deep-agent.md) — durable runs and
  the subagent tree that budgets cover.
