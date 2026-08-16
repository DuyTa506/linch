# Implementing your own seam

Linch ships **mechanisms**, not policy. Every subsystem is a duck-typed protocol you
can re-implement and inject — no base class to inherit, just match the attributes and
methods. This page is the "implement your own" reference for the seams added in the
roadmap, mirroring the patterns already documented for
[`ExecutionBackend`](./tools.md#bash-execution-backend),
[`Tool`](./tools.md#custom-tools), [`MemoryStore` and `ContextBuilder`](./context-and-memory.md),
providers, and [hooks](./hooks.md).

A protocol is satisfied structurally: implement the listed methods with matching
signatures and pass your object where the built-in adapter would go. The runtime
probes optional methods with `getattr`/`hasattr`, so adding extra methods is harmless.

Import supported extension contracts and policy primitives from the top-level package.
For example, custom permission policies can construct their ordered rules without
depending on a private module path:

```python
from linch import BashRule, PathRule, ToolRule
```

Reusable contract checks are available for seams with non-obvious async store
invariants:

```python
import pytest
from linch import (
    assert_file_backend_contract,
    assert_isolation_backend_contract,
    assert_mailbox_contract,
    assert_memory_store_contract,
    assert_schedule_store_contract,
    assert_tool_contract,
)

@pytest.mark.asyncio
async def test_my_file_backend_contract(tmp_path):
    await assert_file_backend_contract(lambda: MyFileBackend(tmp_path / "files.db"))

@pytest.mark.asyncio
async def test_my_isolation_backend_contract(tmp_path):
    await assert_isolation_backend_contract(lambda: MyIsolationBackend(root=tmp_path))

@pytest.mark.asyncio
async def test_my_mailbox_contract(tmp_path):
    await assert_mailbox_contract(lambda: MyMailbox(tmp_path / "mailbox.db"))

@pytest.mark.asyncio
async def test_my_memory_store_contract(tmp_path):
    await assert_memory_store_contract(lambda: MyMemoryStore(tmp_path / "memory.db"))

@pytest.mark.asyncio
async def test_my_schedule_store_contract(tmp_path):
    await assert_schedule_store_contract(lambda: MyScheduleStore(tmp_path / "schedules.db"))

@pytest.mark.asyncio
async def test_my_tool_contract():
    await assert_tool_contract(
        MyTool(),
        valid_input={"query": "contract smoke"},
        invalid_input={},
    )
```

The helpers expect a factory that returns a fresh, empty adapter. They exercise
the same behavioral guarantees Linch's built-in adapters rely on: file backend
CRUD/list/edit semantics, distinct writable isolation workspaces, destructive
mailbox drains, no dropped concurrent sends, memory upsert/search/filter
behavior, schedule add/update/remove/list, atomic `claim_due` when the store
implements it, and custom tool metadata/validation/execution/resource shape.

Copyable starter files live in
[`examples/extensions/`](../../examples/extensions/): provider, memory store,
virtual filesystem backend, tool package, and hook package templates. They are
kept importable and smoke-tested so they track the public protocols.

---

## `IsolationBackend` — where a subagent runs

A subagent can run in an isolated working directory so its file writes never collide
with the parent or a sibling. `tools/isolation.py` ships `TempDirIsolation`; implement
the protocol to point isolation at a container, a remote sandbox, or a pooled workspace.

```python
from typing import Protocol

class IsolationBackend(Protocol):
    async def acquire(self) -> str: ...                      # returns a cwd path
    async def release(self, cwd: str, *, keep: bool = False) -> None: ...
```

- `acquire()` provisions a workspace and returns its `cwd`. The subagent runs there.
- `release(cwd, keep=...)` tears it down. `keep=True` asks the backend to preserve the
  workspace (e.g. on failure, for inspection) instead of deleting it.

```python
class GitWorktreeIsolation:
    def __init__(self, repo: str) -> None:
        self._repo = repo

    async def acquire(self) -> str:
        path = await _make_worktree(self._repo)   # your provisioning
        return path

    async def release(self, cwd: str, *, keep: bool = False) -> None:
        if not keep:
            await _remove_worktree(cwd)
```

Inject it on the subagent path (see [deep-agent](./deep-agent.md)). Honor the contract:
`acquire` must return a usable, writable directory; `release` must be idempotent so a
double-release (abort + normal completion) cannot raise.

---

## `Mailbox` — peer-to-peer messages between agents

The mailbox is the substrate for inter-agent messaging. `InMemoryMailbox` is the
in-process default; implement the protocol for a durable or cross-process inbox (SQLite,
Redis, a queue).

```python
from dataclasses import dataclass, field
from typing import Protocol

@dataclass(slots=True)
class MailboxMessage:
    sender: str
    recipient: str
    content: str
    type: str = "message"
    request_id: str | None = None      # set on a request…
    in_reply_to: str | None = None     # …echoed on its response (see Correlator)
    id: str = field(default_factory=...)

class Mailbox(Protocol):
    async def send(self, message: MailboxMessage) -> None: ...
    async def drain(self, recipient: str) -> list[MailboxMessage]: ...
```

Two invariants your implementation **must** hold:

- **`drain` is destructive and atomic** — a message is delivered to exactly one drain.
  Two concurrent drains of the same inbox must not both see it.
- **Concurrent `send`s never drop** — guard the per-recipient inbox with a lock so
  interleaved sends all land.

`sender`/`recipient` are opaque addresses (a session id, a worker `display_name`, any
handle you choose). `type` is a neutral category *you* interpret. Pair with the
`Correlator` helper to match a `request_id` to its `in_reply_to` response.

---

## `ScheduleStore` — durable time triggers

`SchedulerLoop` fires `Schedule`s (cron or interval) and reads/writes them through a
`ScheduleStore`. `InMemoryScheduleStore` and durable `SqliteScheduleStore` ship;
implement the protocol for another backend (Postgres, a cloud scheduler).

```python
from typing import Protocol

class ScheduleStore(Protocol):
    async def add(self, schedule: Schedule) -> None: ...
    async def update(self, schedule: Schedule) -> None: ...
    async def remove(self, schedule_id: str) -> bool: ...      # True if it existed
    async def get(self, schedule_id: str) -> Schedule | None: ...
    async def list(self) -> list[Schedule]: ...
```

A `Schedule` carries exactly one of `cron` / `interval_s`, a `payload`, an `enabled`
flag, and a computed `next_run` (epoch seconds). Your store only persists and returns
them — `Schedule.compute_next_run()` owns the timing math, so a custom store never
re-implements cron parsing.

For multi-process durability, also guard firing with a lock row so two processes
sharing one store don't double-fire the same schedule. `SqliteScheduleStore` is the
reference for the persistence shape; see [the scheduling section of the roadmap](../ROADMAP.md)
for the loop contract.

---

## `Limiter` — gate every live provider call

Retry handling is reactive: `providers/retry.py` honors `Retry-After` once a 429
has already been returned. A `Limiter` is the proactive counterpart — core holds
one of its slots around every live provider call, so you throttle before the
provider does it for you.

```python
from typing import Protocol

class Limiter(Protocol):
    async def acquire(self, *, model: str) -> None: ...
    def release(self, *, model: str) -> None: ...
```

A plain concurrency cap is six lines:

```python
import asyncio

class SemaphoreLimiter:
    def __init__(self, cap: int) -> None:
        self._sem = asyncio.Semaphore(cap)

    async def acquire(self, *, model: str) -> None:
        await self._sem.acquire()

    def release(self, *, model: str) -> None:
        self._sem.release()

agent = Agent(..., limiter=SemaphoreLimiter(8))
```

Build the semaphore lazily on first `acquire` if the `Agent` may be constructed
outside a running loop. For that common case `Agent(max_provider_concurrency=8)`
is the shortcut — it builds this limiter for you, and passing both raises
`ConfigError`.

**Know your loop affinity.** `asyncio.Semaphore` binds to the loop of its first
*contended* acquire, so the sketch above breaks when an `Agent` is reused from a
second loop — and only once calls start queueing, which makes it a load-only
failure. `max_provider_concurrency` handles this for you: it rebuilds the
semaphore when the loop changed and nothing is held, and raises `ConfigError`
rather than rebuilding while slots are still outstanding, since that would hand
the second loop its own full budget. A limiter of your own that must span
concurrently running loops needs a primitive that is not loop-bound.

`model` is passed so one limiter can keep per-model budgets: a token bucket keyed
by model refills at that model's requests-per-minute and `acquire` waits for a
token. Because `acquire` is awaited, cancellation propagates through it for free
— an aborted run stops waiting rather than holding the queue.

Two placements to know about. The gate is *inside* the turn generator, so it is
released while a same-model retry backs off, and released deterministically when
a caller abandons the run. It also wraps `strategy.compact(...)` at its single
invocation point, so a custom `CompactionStrategy` is bounded too without its
`compact(ctx, provider)` signature knowing about limiters.

The limiter is per-`Agent`, so N agents in one process are independent by
default. Sharing one instance across agents is how you give a tenant a single
budget across several agents — that is a deliberate choice, not an accident.

---

## Reversible registration — every registration returns a `Disposable`

Anything that mutates a registry hands you back a `linch.Disposable` — a single
undo handle — so a host can add a capability for the scope of a task and remove
exactly it afterward, without tracking names.

```python
from linch import Disposable  # only needed for a type annotation

disposer = agent.tools.register(MyTool())   # -> Disposable
...
await disposer.dispose()                     # removes exactly that registration
await disposer.dispose()                     # single-use: a no-op
```

Three properties make this safe to lean on:

- **Idempotent.** A successful `dispose()` is final; a second call does nothing.
- **Identity-checked.** Disposing a tool registration removes the tool only if
  the current entry is still the one that was registered, so a stale disposer
  never clobbers a later `replace`.
- **Retryable on failure.** If teardown raises, the disposer stays live so a
  later close can finish the cleanup.

`register` rejects a duplicate name; use `replace` for a reversible hot swap.
The same contract holds for services registered on an agent's `Context`
(`ctx.register(key, service) -> Disposable`) and for event listeners. To run
setup and teardown together, `ctx.effect(fn)` runs `fn` now and collects the
disposer it returns, disposing it (and everything after it) in reverse order
when the scope closes. See [the kernel](../architecture/kernel.md).

---

## The tool pipeline — wrap `tools/execute`

Every `Agent` owns a `ToolPipeline` (`agent.tool_pipeline`) exposing three seams
around a single tool call — `tools/pre-execute → tools/execute →
tools/post-execute`. This is where cross-cutting behavior (metrics, a
pipeline-scoped deadline, tracing) hangs off, instead of editing the scheduler.

The `tools/execute` seam is a **waterfall**: each wrapper receives a `next` and
must `await next()` to run the wrapped body (the real `tool.execute` is the
innermost call). Wrap around `next()` to add behavior:

```python
import time
from linch import ToolExecution   # public; the payload threaded through the seam

def metrics_wrapper(record):
    async def wrapper(execution: ToolExecution, next):
        start = time.perf_counter()
        error = None
        try:
            return await next()                     # run the wrapped body
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            record(execution.tool_name, time.perf_counter() - start, error)
    return wrapper

disposer = agent.tool_pipeline.on_execute(metrics_wrapper(my_sink))  # -> Disposable
```

`on_pre_execute(listener)` and `on_post_execute(listener)` register the other
two seams; all three return a `Disposable`. Points to know:

- **Zero-overhead when unused.** With no `tools/execute` listener attached the
  scheduler calls `tool.execute` directly — behavior is byte-identical to before
  the pipeline existed.
- **The payload is read-only for authorization.** `ToolExecution` carries
  `tool_name`, `tool_use_id`, `input`, `ctx`, `tool`, plus `signal`/`metadata`
  extension fields. The pipeline runs *after* canonical validation and
  permission, so a listener must **not** rewrite `tool`, `input`, or the
  decision — scheduler dispatch fails closed if it does. An
  authorization-sensitive transform belongs in a `PreToolUse` hook, where the
  input is validated and checked again.
- **Skipping `next()` vetoes.** Returning from a wrapper without awaiting `next()`
  deliberately short-circuits the body — use it to block a call, never by
  accident.

---

## `ExecutionBackend` — one shell + filesystem world

Shell (subprocess) and filesystem used to be unrelated worlds. `ExecutionBackend`
bundles them so a consumer swaps one object and both move together — a duck-typed
protocol, no base class:

```python
from typing import Any, Protocol, runtime_checkable

@runtime_checkable
class ShellBackend(Protocol):
    async def run(self, command: str, *, cwd: str, timeout_s: float,
                  signal: Any = None) -> "ExecResult": ...

@runtime_checkable
class ExecutionBackend(Protocol):
    shell: ShellBackend   # the subprocess half
    fs: "FileBackend"     # the filesystem half (see FileBackend on the tools page)
```

`LocalExecutionBackend` is the default (local subprocess + on-disk files);
`RemoteExecutionBackend(shell=..., fs=...)` bundles transports you supply (e.g. a
sandbox). `BashTool` sources its shell from the backend and the filesystem tools
read its `fs`, so pointing an agent at a sandbox is one argument:

```python
from linch import Agent, LocalExecutionBackend

agent = Agent(..., cwd=".", execution_backend=LocalExecutionBackend(cwd="."))
```

Construct the local world with `cwd=Agent.cwd` so shell and filesystem share one
workspace (a mismatch raises `ConfigError`). A shell-only `execution_backend`
remains a deprecated compatibility path — it has no filesystem transport, and
pairing it with `FeatureFlags(filesystem=True)` requires an explicit
`filesystem=`; migrate to a real `ExecutionBackend`.

**`execution_backend=` transfers ownership.** The agent closes that world during
`Agent.close()`, so do *not* hand the same object to two agents — the first
close leaves the second on closed transports. To share one world, register it on
a `Context` and let each agent inherit it:

```python
host = Context(label="host")
host.register("execution", RemoteExecutionBackend(shell=..., fs=...))

first = Agent(..., context=host)
second = Agent(..., context=host)   # same world, borrowed not owned
await first.close()                 # the shared world stays open
```

An inherited world is borrowed: closing one agent disposes only its own child
scope. This is also how a subagent picks up its parent's world
(`ctx.get("execution")`) unless you give it its own.

---

## System-prompt assembly — ordered static prefix + dynamic blocks

There is deliberately **no** `SystemPromptBuilder` protocol: two existing seams already
cover ordered, cache-aware prompt assembly, and a third would duplicate them.

1. **Static, ordered sections** — `SystemPromptConfig.sections` is a list of
   `SystemPromptSection(name, text, cacheable=True)`. Sections render in order after the
   base prompt. `cacheable=False` marks a section as the end of the cached prefix (it is
   re-sent uncached every turn); leave it `True` for stable text so prompt caching keeps
   the whole prefix warm.

   ```python
   from linch.config import SystemPromptConfig, SystemPromptSection

   agent = Agent(
       ...,
       system_prompt_config=SystemPromptConfig(
           append="You are a research analyst.",
           sections=[
               SystemPromptSection(name="policy", text=POLICY_TEXT),          # cached
               SystemPromptSection(name="tenant", text=tenant_blurb, cacheable=False),
           ],
       ),
   )
   ```

2. **Per-turn dynamic blocks** — a `ContextBuilder` can emit ephemeral *system* blocks
   computed fresh each turn (RAG snippets, recalled memory, live state). These append to
   the provider request only and default to `cacheable=False`. This is the seam for
   anything that changes turn to turn — see [Context & memory](./context-and-memory.md).

Volatile per-turn facts (today's date, recalled memory) are injected as **user**
messages, not the system prompt, so the system prefix stays cacheable. Reach for a
`ContextBuilder` when you need computed-per-turn content; reach for `sections` when you
need ordered static blocks with explicit cache boundaries.

---

## Memory lifecycle — extraction + consolidation

The memory *store* is one seam ([`MemoryStore`](./context-and-memory.md)); the
*lifecycle* — what to remember and when to consolidate — is two more, both policy you
supply over neutral wiring in `memory/lifecycle.py`.

**`MemoryExtractor`** is a plain callable (no base class), run on a terminal turn to
propose what to persist:

```python
from linch import MemoryExtractionContext, MemoryItem

async def my_extractor(ctx: MemoryExtractionContext) -> list[MemoryItem]:
    # ctx.history is the pre-trim full_history tail (the complete record, not the
    # trimmed provider_view); ctx.store lets you read existing entries first.
    facts = await infer_durable_facts(ctx.history)
    return [MemoryItem(id=f.id, content=f.text, metadata={"tier": "semantic"}) for f in facts]
```

Wire it through `MemoryExtractionHook(my_extractor, store=...)` (the hooks layer). The
prompt and what counts as a memory are entirely yours — Linch only runs the callable at
the terminal-turn chokepoint and upserts what it returns.

**`ConsolidationGate`** throttles an expensive consolidation pass on time + change-count
+ an in-process single-flight lock:

```python
from linch import ConsolidationGate

gate = ConsolidationGate(min_interval_s=300, min_changes=20)
gate.record(n)                       # note n memories changed
ran = await gate.run(consolidate)    # runs the thunk iff both gates pass; resets on run
```

`run()` returns `True` only when it actually fired (counters reset) and is single-flight
within the process. A *multi-process* lock (a store lock row) is left to durable store
adapters — add it in your `MemoryStore` if you consolidate from more than one process.

---

## Related pages

- [Tools](./tools.md) — `Tool`, `ExecutionBackend`, `FileBackend` seams.
- [Context & memory](./context-and-memory.md) — `ContextBuilder`, `MemoryStore`.
- [Hooks](./hooks.md) — the single dispatch layer the lifecycle/verifier/observer adapters ride on.
- [Versioning](../versioning.md) — what a protocol change means for semver.
