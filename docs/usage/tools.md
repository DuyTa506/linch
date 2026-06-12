# Tools

[← Usage guide](./README.md)

Tools are how an agent acts on the world: search a knowledge base, read a file,
run a shell command. Linch tools are **duck-typed protocols** — you implement a
small set of attributes and methods, and they drop straight into a
`ToolRegistry`. This page covers writing tools (the `@tool` decorator,
`FunctionTool`, and class-based tools), the scheduler that runs them, timeouts
and retry, the Bash execution backend, shared dependencies, and the permission
rules that govern whether a tool may run at all.

---

## Custom tools

Use `@tool` for the common case. It wraps a sync or async function in a normal
Linch tool object, infers a minimal JSON schema from the function signature,
and injects `ToolContext` when the function asks for `ctx`. You write a plain
Python function — no base class, no manual schema — and the decorator handles the
protocol plumbing.

```python
from linch import Agent, ToolContext, tool
from linch.tools.registry import empty_tools, tools_from_defaults

@tool(description="Search the internal knowledge base.", tags=("rag",))
async def search_kb(query: str, ctx: ToolContext) -> str:
    results = await ctx.deps.kb.search(query)
    return "\n".join(results)

# No built-in tools (pure domain agent)
agent = Agent(..., tools=empty_tools(search_kb), deps=my_app_state)

# SWE tools minus Bash, plus custom
registry = tools_from_defaults(exclude={"Bash"}, extra=[search_kb])
agent = Agent(..., tools=registry, deps=my_app_state)
```

The schema is inferred from your annotations, so name your parameters
descriptively — the model sees them. The `ctx` parameter is special: when you
declare it, the runtime injects a `ToolContext` (see [Dependencies](#dependencies-shared-app-state)
below) instead of treating it as a model-supplied argument. The `@tool`
decorator also accepts `scope`, `parallel`, `tags`, `summary`, `resources`,
`retryable`, and `execution_timeout_ms` to override the defaults discussed
later on this page.

`empty_tools(...)` builds a registry with *only* the tools you pass — the right
choice for a pure domain agent that should never touch the filesystem or shell.
`tools_from_defaults(exclude=..., extra=...)` starts from the standard SWE set
(`Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`) and adds or removes from there.

### FunctionTool

For explicit construction or dynamic registration, use `FunctionTool` directly.
This is the same machinery the `@tool` decorator uses, exposed as a class so you
can build tools at runtime (for example, registering one tool per tenant or per
configured integration):

```python
from linch import FunctionTool, ToolContext

def lookup_customer(customer_id: str, ctx: ToolContext) -> dict:
    return ctx.deps.crm.lookup(customer_id)

customer_tool = FunctionTool(
    lookup_customer,
    name="LookupCustomer",
    description="Look up a customer profile.",
    scope="read",
    parallel=True,
)
```

### Class-based tools

Class-based duck-typed tools remain supported and are the right fit when you
need custom validation, resource declarations, or richer execution behavior.
Reach for a class when a function signature is too coarse — for instance when
you want to reject bad input *before* execution, declare which resources the
tool touches, or return a `ToolResult` with provenance metadata.

```python
from linch.tools.base import ResourceAccess, ToolContext, ToolResult

class MyTool:
    name = "search_kb"
    description = "Search the internal knowledge base."
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    scope = "read"          # "read" | "write" | "exec"
    parallel = True         # can run concurrently when scope is read

    def validate(self, raw: dict) -> dict:
        if not raw.get("query"):
            raise ValueError("query is required")
        return raw

    def resources(self, input: dict) -> list[ResourceAccess]:
        return [ResourceAccess(resource="kb:default", mode="read")]

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        results = await ctx.deps.kb.search(input["query"])  # use deps
        return ToolResult(
            content=results,
            summary=f"search_kb({input['query'][:40]})",
            metadata={"query": input["query"]},
        )

    def summarize(self, input: dict) -> str:
        return f"search_kb({input.get('query','')[:40]})"

# No built-in tools (pure domain agent)
agent = Agent(..., tools=empty_tools(MyTool()))

# SWE tools minus Bash, plus custom
from linch.tools.registry import tools_from_defaults
registry = tools_from_defaults(exclude={"Bash"}, extra=[MyTool()])
agent = Agent(..., tools=registry)
```

The four methods map to the tool lifecycle: `validate()` normalizes/checks the
model's raw input (raise to reject), `execute()` does the work and returns a
`ToolResult`, `summarize()` produces the one-line label shown in events and UIs,
and `resources()` (optional) declares the resources the call reads or writes so
the scheduler can avoid conflicts. The `ToolResult` carries the compact
`content` the model sees plus optional richer fields (`summary`, `metadata`,
`citations`, `attachments`) that host apps can use for provenance and rendering
without bloating the model's context.

---

## The scheduler

The scheduler runs independent read/search tools in parallel, serializes
write/exec tools, respects `Agent(max_tool_concurrency=...)`, and uses optional
`ResourceAccess` declarations to avoid read/write conflicts on the same file,
database, index, or other host-defined resource.

In practice this means: when the model emits several tool calls in one turn,
read-scoped tools marked `parallel=True` fan out concurrently, while
write/exec tools take an exclusive lane so two writes never race. The default
concurrency limit is the CPU count; cap it with `Agent(max_tool_concurrency=...)`
or the `AGENTKIT_MAX_TOOL_CONCURRENCY` environment variable.

`ResourceAccess` is the finer-grained control. Two tools that both declare a
`read` on `"kb:default"` may overlap; a `read` and a `write` on the same
resource are serialized. Use it when distinct tools share an underlying
resource the scope alone cannot express — a search tool and a re-index tool on
the same vector store, for example.

Very large tool results can be offloaded to a virtual filesystem so they do not
flood the model's context — see [result offloading](./filesystem.md).

---

## Timeouts

Set `Agent(tool_timeout_ms=N)` (or env `AGENTKIT_TOOL_TIMEOUT_MS`) for an
agent-wide deadline. A timed-out tool returns `is_error=True` and the run
continues. Per-tool override: set `execution_timeout_ms` as a class attribute on
the tool; `0` opts out of the agent default.

The default is `None` — no timeout, zero overhead, fully backward compatible.
When set, a timeout converts to an `is_error=True` result rather than raising,
so a slow tool never takes down its parallel siblings; the model sees an
actionable message and can react. The typed exception `ToolTimeoutError`
(`kind="tool_timeout"`, `retryable=True`) is available for observers and
policies.

---

## Retry

Pass `Agent(tool_retry=RetryOptions(max_attempts=3, base_delay_ms=50))`
to retry on transient failures. Read-scope tools retry any exception (they are
idempotent); write/exec tools only retry when the tool declares `retryable = True`.
`AbortError` is never retried.

Retry is **side-effect gated** on purpose: re-running a read or search is
harmless, but blindly re-running a write or shell command could duplicate an
effect. So write/exec tools opt in explicitly via the `retryable` attribute (or
the `@tool(retryable=True)` override) only when their effect is genuinely
idempotent. Retries use exponential backoff from `base_delay_ms`.

---

## Bash execution backend

`Bash` uses `LocalBackend` by default. To run Bash through an injected backend,
pass `execution_backend=...` to `Agent`. The agent only replaces an existing
`Bash` tool; it does not add shell access to a custom registry that deliberately
omits `Bash`.

`ToolRule`, `PathRule`, and `BashRule` still decide whether a Bash command is
allowed to run. An execution backend only changes where and how an approved
command runs. `DockerBackend` keeps the historical behavior by default: writable
workspace mount, Docker's default network, no environment forwarding, and a
normal container root filesystem. Its hardening controls are opt-in.

```python
from linch.tools.execution import DockerBackend

agent = Agent(
    ...,
    execution_backend=DockerBackend(
        image="python:3.12-slim",
        network="none",
        read_only_root=True,
        workspace_mount="rw",
        tmpfs=("/tmp:rw,noexec,nosuid,nodev,size=64m",),
        forward_env=(),
    ),
)
```

Use `workspace_mount="ro"` for read-only workspace inspection, `env={...}` for
explicit container environment variables, `forward_env=(...)` to allowlist host
environment variables, and `user="1000:1000"` for non-root container execution.

`DockerBackend` is guarded by `shutil.which("docker")` (no Docker SDK
dependency). Both backends route timeout and abort through the same path, so
`session.abort()` interrupts an in-flight command rather than leaking a runaway
process. The backend is purely about *where and how* an approved command runs —
**whether** it runs is decided by [permissions](#permissions).

---

## Dependencies (shared app state)

Dependencies are arbitrary app objects — a DB connection, vector store, API
client, config dict — that your tools reach through `ctx.deps`. This is the
single seam for handing live infrastructure to tool code without globals.

```python
# Anything: a DB connection, vector store, API client, config dict
agent = Agent(..., deps={"db": my_db, "vector_store": vs})

# Access inside a class-based tool:
async def execute(self, input, ctx: ToolContext) -> ToolResult:
    results = await ctx.deps["vector_store"].search(input["query"])
    ...

# Or inside a function tool:
from linch import tool

@tool
async def search_docs(query: str, ctx: ToolContext) -> str:
    return await ctx.deps["vector_store"].search(query)

# Override per-run (e.g. tenant-specific connection)
from linch import RunOptions
async for event in session.run("...", RunOptions(deps=tenant_db)):
    ...
```

`deps` can be any object — a dict, a dataclass, or your own app-state class.
Tools that ask for `ctx` receive it on every `execute()` call. The per-run
override (`RunOptions(deps=...)`) is the multi-tenant pattern: one shared
`Agent`, but each run scoped to a tenant-specific connection or context. The
same `deps` object also drives request-scoped context building, so memory and
RAG recall can share the exact backends your tools use — see
[Context & memory](./context-and-memory.md).

---

## Permissions

Permissions gate every tool call *before* execution. Rules are evaluated in
order; a matching `deny` blocks the call, and anything not auto-approved either
prompts via `canUseTool` or pauses the loop with a `PermissionRequestEvent`.

```python
from linch.permissions import ToolRule, PathRule, BashRule

agent = Agent(
    ...,
    permissions={
        "mode": "acceptEdits",          # auto-allow file edits; ask for Bash
        "rules": [
            ToolRule(tool="Bash", decision="deny"),              # block Bash entirely
            PathRule(paths=["/secrets/**"], decision="deny"),    # block secret paths
            BashRule(patterns=["rm -rf*"], decision="deny"),     # block dangerous commands
        ],
        "canUseTool": my_approval_callback,   # async or sync
    },
)
```

The three rule types target different things:

| Rule | Matches on | Notes |
|---|---|---|
| `ToolRule` | Tool name | Allow/deny a whole tool, e.g. block `Bash` entirely. |
| `PathRule` | File paths (glob) | `*`/`?` do **not** cross `/`; `**` does. Anchored regex match. |
| `BashRule` | Bash command (fnmatch-glob + token-prefix) | Prefix/glob match on the command — no substring matching. |

`mode` sets the baseline before rules apply:

- `"default"` — prompt the user for anything not explicitly allowed.
- `"acceptEdits"` — auto-allow file edits, prompt for the rest (e.g. Bash).
- `"skip-dangerous"` — allow all (use only for trusted, headless runs).

`canUseTool` is your interactive approval callback (sync or async). When a tool
call is not auto-approved and no callback resolves it, the loop emits a
`PermissionRequestEvent` and pauses until the caller responds — see
[Events](./events.md). Resolved allow/deny decisions are persisted into the run
checkpoint and replayed on resume, so a durable HITL flow does not re-prompt for
a decision the user already made.

To transform or intercept tool calls and results programmatically (rather than
just allow/deny), use the `PreToolUse` / `PostToolUse` chokepoints and
`ToolMiddlewareHook` documented in [Hooks](./hooks.md).

---

## Related pages

- [Hooks](./hooks.md) — `PreToolUse`/`PostToolUse` and tool middleware.
- [Virtual filesystem](./filesystem.md) — offloading large tool results.
- [Context & memory](./context-and-memory.md) — the same `deps` drive context.
- [Events](./events.md) — tool-call and permission events.
