# Kernel

> Part of the [Linch architecture guide](./README.md).

The kernel (`src/linch/kernel/`) is a small, dependency-free "Cordis-lite" layer
that gives Linch three things the rest of the SDK builds on: **reversible
effects**, a **typed event bus**, and **per-agent `Context` scopes** (a scoped
IoC container). It is async-first and ships no third-party dependency.

Public names: `linch.Context`, `linch.Disposable`. `EffectScope`, `EventBus`,
and `Next` live under `linch.kernel` for internal use.

Why it exists: extension points (tool wrappers, capability providers, listeners)
must be **addable and removable** without editing the loop, and N agents must run
in one process without sharing mutable globals. The kernel makes "register →
returns a `Disposable`" and "each agent owns a scope" the uniform mechanism.

---

## Disposable — reversible effects

`Disposable` (`kernel/effects.py`) wraps an undo function. `await d.dispose()`
(or `await d()`) runs one teardown attempt at a time. Successful teardown is
final and later calls are no-ops; a failed teardown remains retryable, so a
later close can finish cleanup. The undo may be a coroutine. Every registration
in the system returns one:

```python
disposer = registry.add(tool)       # returns a Disposable
...
await disposer.dispose()            # removes exactly that tool
await disposer.dispose()            # single-use: a no-op
```

Reversal is **identity-checked**: disposing a tool registration only removes the
tool if the current entry is still the one that was registered (so disposing a
stale registration never clobbers a replacement).

`EffectScope` collects disposers and tears them down in **reverse order**. It
attempts every disposer; successful entries stay complete while failed entries
remain for a later retry.
`ctx.effect(fn)` runs `fn` immediately and registers whatever disposer it returns
(or yields), so setup and teardown live together instead of in a distant
`finally`.

## EventBus — emit / serial / waterfall

`EventBus` (`kernel/events.py`) registers listeners with
`on(name, listener, *, prepend=False) -> Disposable` and dispatches three ways:

| Dispatcher | Semantics |
|---|---|
| `emit(name, *args)` | Fire-and-forget. Non-vetoing; each listener is isolated by its own `try/except` so one failure never breaks the others. Coroutine listeners are scheduled. |
| `serial(name, *args)` | Awaits listeners in order and **bails on the first** non-`None`/non-`False` return (that value is the result). |
| `waterfall(name, *args, next)` | Folds listeners innermost→outermost. Each listener is `async def listener(*args, next)` and MUST `await next()` to delegate inward; **not** calling `next()` deliberately vetoes/short-circuits. |

Pick the dispatcher that matches intent: `emit` for observation, `serial` for
"first responder wins", `waterfall` for around-wrappers (timeout, retry, metrics)
that need to wrap the inner call.

## Context — per-agent scope

`Context` (`kernel/context.py`) bundles a service registry, an `EventBus`, and an
owning `EffectScope`. Each `Agent` owns one; shared services are resolved through
it rather than through module globals — that is what makes Linch multi-tenant
safe.

- `register(key, service) -> Disposable` — add a service; dispose to remove it.
- `ctx.<name>` — read a **required** capability as an attribute (e.g.
  `ctx.execution`); `AttributeError` if unresolved. Underscore names raise, to
  avoid recursion.
- `ctx.get(name, default)` — the same lookup, made **optional**: *default* when
  unresolved. Both walk up to parent scopes, so a child sees a service its
  parent registered unless it shadows the name.
- `on(...)` / `effect(...)` — delegate to the bus / scope.
- `scope(label) -> Context` — a **child** sharing the bus but with an isolated
  service overlay and its own child `EffectScope`, so per-agent (or per-subagent)
  teardown disposes only that branch.

`Agent` creates one `Context` for its lifetime (or accepts a supplied one), then
registers the agent's pipeline and effective execution world in that scope.
The Agent owns the supplied scope and disposes it during close; hosts that need
to retain a parent scope should pass `parent.scope("agent")` rather than the
parent itself. Sessions and child agents resolve capabilities through the scope;
they do not use process-global services. `await agent.close()` first quiesces
the agent and then disposes the context. Context teardown is transactional:
successful registrations are not repeated, while a failed disposer remains
retryable and a later `close()` resumes the unfinished teardown.

---

## What builds on the kernel

- **Reversible registration** — `ToolRegistry.add/register/replace` return a
  `Disposable` (see [Tool Protocol](./tool-protocol.md)).
- **Open tool pipeline** — `ToolPipeline` (`tools/pipeline.py`) exposes
  `tools/pre-execute → tools/execute → tools/post-execute`. `tools/execute` is a
  **waterfall** so wrappers (`tools/wrappers/`: metrics, timeout) wrap the inner
  dispatch. Every `next` handle is single-use: a wrapper must await it exactly
  once, and omitting it intentionally short-circuits. The scheduler routes each
  call through the pipeline only when a listener is attached; otherwise it calls
  `tool.execute` directly, so behavior is byte-identical when unused. Active
  listeners need stable policy identity for durable resume.
  The pipeline runs after canonical input validation and permission evaluation;
  its listeners must treat `ToolExecution.input`, `tool`, and authorization
  decisions as immutable. Authorization-sensitive rewrites belong in
  `PreToolUse`, where input is validated and checked again before permission.
- **Capability seam** — `ExecutionBackend` (`execution/backend.py`) bundles a
  `shell` and a `fs` (filesystem) transport as one world; `LocalExecutionBackend`
  is the default provider and `RemoteExecutionBackend` bundles supplied
  transports. Consumers (e.g. `BashTool`) depend on the seam, not a concrete
  backend (Definition → Provider → Consumer).
- **Event forward-compat** — unknown persisted events tagged `ignorable` round-
  trip through an `IgnorableEvent` sentinel instead of aborting load (see
  [Event Taxonomy](./events.md)).

## Design rationale

- **Registrations are effects.** A feature that can be turned on must be turnable
  off; returning a `Disposable` makes reversal uniform and idempotent instead of
  bespoke per registry.
- **Extend on the bus, don't edit the loop (Open–Closed).** New tool behavior is
  a listener, not a new branch threaded through `scheduler.py`.
- **Per-agent scope, not a global.** The `Context` is the multi-tenant mechanism:
  shared state is scoped to an agent tree and disposed with it.

---

Back to the [architecture index](./README.md).
