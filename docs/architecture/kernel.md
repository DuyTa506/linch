# Kernel

> Part of the [Linch architecture guide](./README.md).

The kernel (`src/linch/kernel/`) is a small, dependency-free "Cordis-lite" layer
that gives Linch three things the rest of the SDK builds on: **reversible
effects**, a **typed event bus**, and **per-agent `Context` scopes** (a scoped
IoC container). It is async-first and ships no third-party dependency.

Public names: `linch.Context`, `linch.Disposable`, and `linch.Plugin`.
`EffectScope`, `EventBus`, and `Next` live under `linch.kernel` for internal use.

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
disposer = registry.register(tool)  # returns a Disposable
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
  Registration and removal publish `context/service` with `(context, key)` on
  the shared bus after the registry changes. Removal awaits every listener;
  failed notifications remain retryable through the same disposer. With no
  listeners, no event is dispatched.
- `ctx.<name>` — read a **required** capability as an attribute (e.g.
  `ctx.execution`); `AttributeError` if unresolved. Underscore names raise, to
  avoid recursion.
- `ctx.get(name, default)` — the same lookup, made **optional**: *default* when
  unresolved. Both walk up to parent scopes, so a child sees a service its
  parent registered unless it shadows the name.
- `on(...)` / `effect(...)` — delegate to the bus / scope.
- `inbox(producer) -> Disposable` — add an ordered, agent-local run-boundary
  producer. The producer is called as `producer(session, run_id)` and returns an
  async iterator of `Message` or already-committed `Event` values. Messages are
  appended to session history and emitted as notification `UserEvent`s; Events
  are emitted/persisted without another history append. The registration is
  independent and idempotently removable, and an iterator's `aclose()` is
  awaited when present. No producer means the loop does not allocate or dispatch
  an inbox coroutine.
- `scope(label, isolate_events=False) -> Context` — a **child** sharing the bus but with an isolated
  service overlay and its own child `EffectScope`, so per-agent (or per-subagent)
  teardown disposes only that branch. With `isolate_events=True`, listener
  registrations are snapshotted onto an independent bus; filtered parent service
  changes still reach the child. `EventBus.fork()` and `ToolPipeline.fork()` own
  the listener-copy mechanics and preserve registration ordering and ownership.

`Agent` creates one owned child `Context` for its lifetime. When `context=` is
supplied it is the parent/host scope, which remains alive when the Agent closes;
hosts may also pass `parent.scope("agent")` when they want an explicit child.
The Agent registers its pipeline and effective execution world in its owned
scope. Sessions and child agents resolve capabilities through the scope;
they do not use process-global services. `await agent.close()` first quiesces
the agent and then disposes the context. Context teardown is transactional:
successful registrations are not repeated, while a failed disposer remains
retryable and a later `close()` resumes the unfinished teardown.

### Plugins

`Plugin` is a duck-typed protocol with `name`, `version`, `provides`,
`requires`, and synchronous `setup(ctx, config) -> Disposable`. Mount one with
`await agent.use(plugin, config)`. A mount is dormant until every required
service resolves in the agent context; it gets a fresh child scope for each
activation. Removing a requirement withdraws published services before awaiting
cleanup, and a later reappearance activates a fresh scope. The returned
`Disposable` unmounts the mount and retries failed teardown on a later dispose
or `agent.close()`. A synchronous `Context.register` emits its change event
immediately; activation reconciliation runs asynchronously, while removal waits
for dependent cleanup.

Only services declared in `provides` may be registered by a plugin, including
nested scopes. Services are not published to the agent until setup and the
audit complete, so failed setup is fully rolled back. Plugin mounts and inbox
registrations are per-agent; sharing a parent `Context` does not share a
mount's child services.

### Domain profiles

`Profile` (`profiles.py`) composes immutable declarative fields and plugin rows
by stable id. `RunOptions(profile=...)` selects the composed definition. Before
the loop starts, the session enters a run scope: a shallow configuration copy of
the same Agent shares its provider, stores, and host dependencies, while tools,
permission configuration, hook registrations, tool pipeline listener lists,
turn-stopping registrations, and context event listener lists are local to the
run. `agent.use` mounts selected plugins in that scope. Existing hooks and host
service objects retain their normal shared ownership. No listener is registered
unfiltered on the parent agent bus.

The effective tool registry intersects the profile allow/deny names and any
session tool override. A deny-only permission layer reinforces the restriction;
host permission rules still decide authorization. Role instructions append to
the existing system prompt, schemas use `OutputSchema`, and budget limits create
a fresh `RunBudget`. Cleanup restores the session's original Agent and overrides
and disposes the scope, including on early iterator close or setup failure.
Detached work releases the scope when its last task finishes; retained child
sessions retain their services until released. Session close notifications and
task callbacks trigger disposal without requiring Agent shutdown. The parent
Context retains retry ownership if disposal fails. Inherited service changes
are relayed by the kernel's isolated scope so run-local plugin dependencies
still activate and deactivate normally.

Direct callers pass the same profile on resume; workers resolve it from their
supplied `profiles` definitions on queue launch and takeover. `loop/contracts.py` records its id, version, and
SHA-256 content hash in `RunContract.payload.run_options.profile`. The existing
whole-contract compatibility check rejects omission or a changed definition,
including plugin execution order. `profile=None` adds no contract fields or
request changes. See [domain profiles](../usage/profiles.md) for the API.

---

## What builds on the kernel

- **Reversible registration** — `ToolRegistry.register/replace` return a
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
