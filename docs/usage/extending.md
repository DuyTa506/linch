# Implementing your own seam

Use this page when you need to implement and inject your own runtime seam.

Linch exposes runtime seams you can implement and inject. Most use duck-typed
protocols: match their attributes and methods. Built-in providers instead subclass
`BaseProvider`, an abstract base class; custom providers can also satisfy the runtime
interface structurally. This page complements the patterns documented for
[`ExecutionBackend`](./tools.md#bash-execution-backend),
[`Tool`](./tools.md#custom-tools), [`FileBackend` and `ContextBuilder`](./context-and-memory.md),
providers, and [hooks](./hooks.md).

A protocol is satisfied structurally: implement the listed methods with matching
signatures and pass your object where the built-in adapter would go. The runtime
probes optional methods with `getattr`/`hasattr`; implement optional capabilities
according to their documented semantics.

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
async def test_my_tool_contract():
    await assert_tool_contract(
        MyTool(),
        valid_input={"query": "contract smoke"},
        invalid_input={},
    )
```

The backend, isolation, and mailbox helpers expect a factory that returns a
fresh, empty adapter; `assert_tool_contract` takes a tool instance. They exercise
the same behavioral guarantees Linch's built-in adapters rely on: file backend
CRUD/list/edit semantics, distinct writable isolation workspaces, destructive
mailbox drains, no dropped concurrent sends, and custom tool
metadata/validation/execution/resource shape.

Copyable starter files live in
[`examples/extensions/`](../../examples/extensions/): provider and tool package
templates. They are kept importable and smoke-tested so they track the public
protocols.

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

Inject it on the subagent path (see [deep-agent](./deep-agent.md)). `acquire` must
return a usable, writable directory. Making `release` idempotent is recommended
so repeated cleanup is safe; the protocol and contract helper do not require it.

---

## `Mailbox` — peer-to-peer messages between agents

The mailbox is the substrate for inter-agent messaging. `InMemoryMailbox` is the
only built-in implementation, with process-lifetime state. Implement the
protocol for a durable or cross-process inbox in host-owned infrastructure;
mailbox adapters are outside the core SDK.

```python
from linch import Mailbox, MailboxMessage

message = MailboxMessage(
    sender="planner",
    recipient="worker",
    content="Inspect the request handler.",
)
await mailbox.send(message)
messages = await mailbox.drain("worker")
```

`Mailbox` defines `async send(message) -> None` and
`async drain(recipient: str) -> list[MailboxMessage]`. `MailboxMessage` generates
an `id`; its optional fields default to `type="message"`, `request_id=None`,
and `in_reply_to=None`.

Two invariants your implementation **must** hold:

- **`drain` is destructive and atomic** — a message is delivered to exactly one drain.
  Two concurrent drains of the same inbox must not both see it.
- **Concurrent `send`s never drop** — guard the per-recipient inbox with a lock so
  interleaved sends all land.

`sender`/`recipient` are opaque addresses (a session id, a worker `display_name`, any
handle you choose). `type` is a neutral category *you* interpret. Pair with the
`Correlator` helper to match a `request_id` to its `in_reply_to` response.

---

## Scheduling and queue workers

Durable schedules and run dispatch are provided by the optional `linch.worker`
package. Hosts register its schedule tools and configure PgQueuer; see
[Scale out](./production.md#scale-out).

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

A plain concurrency cap is short:

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

<a id="reversible-registration--every-registration-returns-a-disposable"></a>

## Reversible registration — register and replace return a `Disposable`

`ToolRegistry.register()` and `replace()` return a `linch.Disposable` undo
handle, so a host can add a tool for the scope of a task and remove that
registration afterward. `unregister()` instead returns the removed tool or `None`.

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

- **Zero-overhead when unused.** With no listeners in any of the three phases, the
  scheduler calls `tool.execute` directly.
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

`ExecutionBackend` bundles shell execution and filesystem access. Swapping one
object changes both together. It is a duck-typed protocol, with no base class:

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

`LocalExecutionBackend` provides local subprocesses and on-disk files; a bare
`Agent` does not implicitly receive this execution authority.
`RemoteExecutionBackend(shell=..., fs=...)` bundles transports you supply (e.g. a
sandbox). `BashTool` sources its shell from the backend and the filesystem tools
read its `fs`, so pointing an agent at a sandbox is one argument:

```python
from linch import Agent, LocalExecutionBackend

cwd = "."
agent = Agent(..., cwd=cwd, execution_backend=LocalExecutionBackend(cwd=cwd))
```

Construct the local world with `cwd=Agent.cwd` so shell and filesystem share one
workspace (a mismatch raises `ConfigError`). `execution_backend` must expose
both `.shell` and `.fs`; shell-only values are rejected. Pair a shell transport
such as `DockerBackend` with a filesystem transport for the same execution
world through `RemoteExecutionBackend(shell=..., fs=...)`. An explicit
`filesystem=` must be the backend's own `.fs` object.

**`execution_backend=` transfers ownership.** The agent closes that world during
`Agent.close()`, so do *not* hand the same object to two agents — the first
close leaves the second on closed transports. To share one world, register it on
a `Context` and let each agent inherit it:

```python
from linch import Context, RemoteExecutionBackend

host = Context(label="host")
host.register("execution", RemoteExecutionBackend(shell=..., fs=...))

first = Agent(..., context=host)
second = Agent(..., context=host)   # same world, borrowed not owned
await first.close()                 # the shared world stays open
```

An inherited world is borrowed: closing one agent disposes only its own child
scope. This is also how a subagent picks up its parent's world
(`ctx.get("execution")`) unless you give it its own.

### Local shell environment and output

`LocalBackend` forwards only `PATH`, `HOME`, `TERM`, `LANG`, `LC_ALL`,
`LC_CTYPE`, `USER`, `SHELL`, and `TMPDIR` by default. Configure
`LocalBackend(env={...}, forward_env=(...))` for additional values; explicit
`env` entries override forwarded values. This is
credential hygiene, not a security boundary. Durable runs with non-empty `env`
or `forward_env` require a caller-owned `resume_fingerprint_key` of at least
16 bytes; values enter resume identity through keyed HMACs, as with Docker.
Default allowlist names enter the policy identity; their ambient values remain
host state. Pin relevant defaults explicitly when a resume must reject changes.

Local and Docker commands receive EOF on stdin; they do not support interactive
input. After the shell exits, pipe readers drain for up to 0.25 seconds, then
close even if a child still holds a pipe open. A child can outlive its foreground
shell; managed runs track its process group for cleanup.

Each stdout/stderr stream retains the first 4,000 and last 26,000 bytes.
Oversized streams spill in full to `{cwd}/.linch/output/<id>-<stream>.log`,
created exclusively with mode `0600`; the directory gets a `.gitignore` containing
`*`. Spill is best effort: a write failure or a stream exceeding 64 MiB discards
the spill file and continues with bounded memory. The preview adds an omission
marker, so its rendered length can exceed the retained-byte limit. Managed-run
cleanup removes tracked shell spill files; read them during the same run. If
cleanup fails, retry session close.

### Optional live process handles

`ShellStarter` is an optional capability alongside the run-only `ShellBackend`.
Its `start(command, cwd=...)` returns a `ShellProcess`; `read()` returns new
stdout/stderr and marks lost bytes with `ShellRead.lossy`, `wait(timeout_s)`
returns `None` while the process is running, and `kill()` terminates its process
group. Local and Docker implement this capability. A third-party backend with
only `run()` remains supported, with its own timeout behavior.

Bash reports available partial output on timeout and abort. It normally reserves
one second from the scheduler's deadline for cleanup (half the deadline when it
is under two seconds). A process resisting SIGTERM can extend completion beyond
that deadline while the owned three-second grace period finishes before SIGKILL.
The original timeout/abort cause remains the result. See
[managed Bash processes](./tools.md#executionbackend--bash-execution) for limits
and lifetime rules.

---

## System-prompt assembly — ordered static prefix + dynamic blocks

System-prompt assembly uses two seams for static sections and dynamic context:

1. **Static, ordered sections** — `SystemPromptConfig.sections` is a list of
   `SystemPromptSection(name, text, cacheable=True)`. Placement defaults to
   `"before_defaults"`; `"after_defaults"` and `"after_env"` are also supported.
   Sections retain declaration order within each placement. For Anthropic, the
   last cacheable system block sets the breakpoint: `cacheable=False` alone
   does not exclude a section if a later block is cacheable.

   ```python
   from linch import SystemPromptConfig, SystemPromptSection

   agent = Agent(
       ...,
       system_prompt_config=SystemPromptConfig(
           append="You are a research analyst.",
           sections=[
               SystemPromptSection(name="policy", text="Cite sources for factual claims."),
           ],
       ),
   )
   ```

2. **Per-turn dynamic blocks** — a `ContextBuilder` can emit ephemeral *system* blocks
   computed fresh each turn (RAG snippets, recalled memory, live state). These append to
   the provider request only and are forced to `cacheable=False`. This is the seam for
   anything that changes turn to turn — see [Context & memory](./context-and-memory.md).

The date is generated for each user message. Recalled memory and other context
can be injected through dynamic system blocks. Reach for a
`ContextBuilder` when you need computed-per-turn content; reach for `sections` when you
need ordered static blocks with explicit cache boundaries.

---

## File memory policy

File memory intentionally has no extraction hook or memory-store contract. Use
the public `FileMemoryContextBuilder` with `ContextInjectionHook`, and keep the
run's `ScopedFileBackend` in `RunOptions(deps={"filesystem": scoped_backend})`.
The builder reads `/memories/MEMORY.md` once per run and includes its first
200 lines; it does not write memory. The model can maintain the file through
filesystem tools, and application policy can update or delete stale content
through the backend. Keep `MEMORY.md` as a one-entry-per-file index; this is
prompt guidance rather than a format enforced by the builder.
For semantic recall, adapt the recipe in
[`examples/memory/mem0_recall.py`](../../examples/memory/mem0_recall.py) with a
fake async client in unit tests.

---

## Related pages

- [Tools](./tools.md) — `Tool`, `ExecutionBackend`, `FileBackend` seams.
- [Context & memory](./context-and-memory.md) — `ContextBuilder`, file memory, and scoped backends.
- [Hooks](./hooks.md) — the single dispatch layer for lifecycle and policy adapters.
- [Versioning](../versioning.md) — what a protocol change means for semver.
