# Minimal production wiring

This page is the short version of embedding Linch inside a long-running async
service: persistence, streaming, human-in-the-loop, resume after a restart,
graceful shutdown, and blast-radius controls. Each section links to the page
with the full detail — this is the wiring checklist, not a re-explanation.

Linch is a **library**, not a daemon. Your service owns the process, the request
lifecycle, and the schedule; Linch owns one agent run at a time.

## 1. Build the agent once, reuse it

An `Agent` holds configuration and its own registries — construct it at startup
and share it across requests/tenants. There is no process-global mutable state,
so independent `Agent` instances run concurrently in one process (multi-tenant
safe).

```python
from linch import Agent, SqliteRunStore
from linch.sessions import SqliteSessionStore

agent = Agent(
    model="claude-opus-4-8",
    session_store=SqliteSessionStore("var/sessions.db"),  # conversation state
    run_store=SqliteRunStore("var/runs.db"),              # checkpoints for resume
    cwd="var/workspace",
)
```

- `session_store` persists conversation state; `run_store` persists per-run
  checkpoints (required for resume). In-memory variants (`InMemorySessionStore`,
  `InMemoryRunStore`) are for tests. Postgres-backed stores ship under the
  `[postgres]` extra.
- See [Agent & session](./agent.md).

## 2. Stream events to your host

A run is an async generator of typed events. Forward them to your UI/log/queue
as they arrive — do not wait for the run to finish.

```python
from linch import ResultEvent

session = await agent.session()
async for event in session.run(user_prompt):
    await publish(event)            # your transport
    if isinstance(event, ResultEvent):
        final = event.final_text
```

See [Events](./events.md). For a compact post-run health surface (slow/failing
tools, context pressure, cost, recovery counters) build a
[`RunReport`](./events.md).

## 3. Human-in-the-loop, durably

When a tool needs approval the stream yields a `PermissionRequestEvent` and the
run pauses with a checkpoint already written. Persist the decision through your
own UI, then resume — the decision replays and the tool is not re-prompted.

```python
from linch import PermissionRequestEvent

async for event in session.run(prompt):
    if isinstance(event, PermissionRequestEvent):
        run_id = session.active_run_id     # capture before the loop ends
        # ... ask a human out-of-band, store allow/deny against run_id ...
```

Permissions and durable decisions: [Hooks](./hooks.md) and the permissions
section of [Agent & session](./agent.md).

## 4. Resume after a restart

With a `run_store`, a run interrupted by a deploy or crash resumes from its last
checkpoint. The continuation is identical (completed tool calls are not re-run,
permission decisions replay).

```python
async for event in session.resume(run_id):
    await publish(event)
```

## 5. Shut down without leaking work

On shutdown, `await agent.close()` cancels background workers/tools and flushes
observers (e.g. OpenTelemetry exporters). To stop a single in-flight run, call
`session.abort()` — a cooperative tool sees `ctx.signal` and the run ends with an
`aborted` result.

```python
try:
    await serve_forever()
finally:
    await agent.close()
```

## 6. Bound the blast radius

These are mechanisms; you supply the policy.

- **Budget** — `RunOptions(budget=RunBudget(max_tokens=..., max_cost_usd=...))`
  caps a run and every subagent it spawns. See [Agent & session](./agent.md).
- **Loop guard** — on by default; stops runaway tool/failure streaks.
- **Permissions** — path/bash/tool rules; default-deny where it matters.
- **Timeouts & retries** — `tool_timeout_ms`, `tool_retry` per tool.
- **Truncation recovery** — opt in with `truncation_recovery=` (off by default;
  Linch never escalates output caps implicitly). See [Agent & session](./agent.md).
- **Redaction** — attach a `RedactionHook` to scrub tool results / final answers
  with host-supplied patterns. See [Hooks](./hooks.md).

## 7. Recurring work

For cron/webhook/interval/CI-driven recurring work, drive
`LoopRunner.run_once()` from your host scheduler — see
[Outer loop runner](./loop-runner.md) and
[`examples/recipes/runner_recipes.py`](../../examples/recipes/runner_recipes.py).

## Optional dependencies are opt-in

The core install pulls only the OpenAI SDK, PyYAML, and `typing_extensions`.
Everything else is an extra and fails with an explicit
`pip install 'linch[...]'` message when missing:

| Extra | Enables |
|---|---|
| `linch[anthropic]` | Anthropic provider |
| `linch[gemini]` | Gemini provider |
| `linch[mcp]` | MCP tool servers |
| `linch[otel]` | OpenTelemetry observer |
| `linch[postgres]` | Postgres session/memory stores |

---

## Related pages

- [Agent & session](./agent.md) · [Events](./events.md) · [Hooks](./hooks.md)
- [Outer loop runner](./loop-runner.md) · [Coordination](./coordination.md)
- [Extending](./extending.md) · [Providers](./providers.md)
