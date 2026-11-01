# Tool Protocol

> Part of the [Linch architecture guide](./README.md).

For normal application tools, prefer the `@tool` helper. It wraps a sync or
async Python function in a regular Tool-compatible object, infers a minimal JSON
schema, injects `ToolContext` when the function asks for `ctx`, and converts
plain return values into `ToolResult`.

```python
from linch import ToolContext, tool

@tool(description="Search the product knowledge base.", tags=("rag",))
async def search_kb(query: str, ctx: ToolContext) -> str:
    return await ctx.deps.kb.search(query)
```

The runtime still consumes the same **duck-typed** protocol — no base class, no
`isinstance` check anywhere in the core. `FunctionTool` implements this shape,
and advanced tools can implement it directly:

```python
class MyTool:
    name: str                                      # unique registry key
    description: str                               # shown to the model
    input_schema: dict                             # JSON Schema object
    scope: Literal["read", "write", "exec"]
    parallel: bool | Callable[[dict], bool]         # concurrency declaration

    # Optional Phase-11 reliability attributes (all duck-typed via getattr)
    execution_timeout_ms: float                    # per-tool timeout; 0 = opt-out
    retryable: bool                                # opt write/exec tool into retry
    always_load: bool                              # keep in deferred catalogs when truthy

    def validate(self, raw: dict) -> dict: ...
    def resources(self, input: dict) -> list[ResourceAccess]: ...
    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult: ...
    def summarize(self, input: dict) -> str: ...   # one-line for logs

    # Optional prompt guidance while this tool is active.
    system_prompt_sections: Iterable[SystemPromptSection] | Callable[[], Iterable[SystemPromptSection]]
```

Tools may opt into Canonical Tool Output V2 by declaring `output_schema`. A V2
tool returns a JSON value, `ToolOutput`, or `ToolOutputError`; it must not return
legacy `ToolResult`. Linch strictly rejects cycles, non-finite floats,
non-string object keys, and arbitrary Python objects, validates successful
values with JSON Schema Draft 2020-12, then renders the provider projection.
Strings render unchanged; other values use compact deterministic JSON. A custom
synchronous renderer must declare stable `renderer_id` and `renderer_version`.
`ToolCallEndEvent.tool_output` carries the canonical value while the existing
`result`, `tool_result`, and `ToolResultBlock` projections remain available.

`ToolContext` carries: `cwd`, `session_id`, `run_id`, `session_store`, `signal`
(abort), `file_read_tracker`, `deps`, `filesystem`, `idempotency_key`, and a
best-effort progress sink. Tools report transient progress with
`ctx.report_progress(message, data=None)`. Progress becomes a `ToolProgressEvent` for the
live consumer only; it is not provider history, a tool result, or a durable run
event.

`deps` is threaded from `Agent(deps=...)` or overridden per-run with `RunOptions(deps=...)`. Use it to inject app state into tools without globals.

`idempotency_key` is a stable string derived from the run id and tool-use id
(`f"{run_id}:{call.id}"`). Durable tool execution is **at-least-once** — a side
effect that happens before its completion record is durable can replay on
resume. A mutating integration can pass this key to an external system (or key
its own dedup table on it) to deduplicate or reconcile an interrupted intent.
The key is stable across resume for the same tool call; Linch does not itself
promise exactly-once execution across separate session stores, run stores, and
external systems.

### Tool-call safety order

The scheduler resolves one canonical input before execution:

1. decode the provider's raw tool call;
2. run the optional `tools/resolve` serial veto with the tool name, raw input,
   and session; the first nonempty reason returns an immediate tool error;
3. resolve and validate the tool call, then reject it unless that tool was
   offered in the current provider request;
4. run `PreToolUse` hooks, which may transform or short-circuit it;
5. validate the transformed input again;
6. evaluate rules and `can_use_tool` on that final input;
7. persist resolved decisions and execute the tool.

The `tools/resolve` callback treats raw input as read-only and runs before tool
validation, hooks, and permissions. A shared parent `Context` may share its
event bus across agents, so listeners filter on the supplied session (the
default tool-search listener checks the owning agent) before returning a veto.

Approval callbacks cannot return Linch 1.x's `updatedInput` rewrite. Input
mutation belongs in `PreToolUse`, where it is revalidated and permission
checked; this prevents approving one path or command and executing another.

`BashTool` delegates command execution to a backend. `LocalBackend` preserves
the default local subprocess behavior with timeout cleanup; `DockerBackend`
uses `docker run --rm` when the Docker daemon is available. Passing
`Agent(execution_backend=...)` replaces an existing `Bash` tool only, so a
restricted registry that omits `Bash` does not gain shell access.

`Agent` requires an `ExecutionBackend` exposing both `.shell` and `.fs`, so shell and
filesystem operations share one coherent execution world.
`LocalExecutionBackend` supplies that pair locally; `RemoteExecutionBackend`
bundles host-supplied transports. Confinement metadata is declarative: a remote
backend is described as sandboxed only when the embedder supplies non-empty
confinement metadata, and the SDK does not independently verify that boundary.
The effective session `cwd` is passed to the shell transport; a remote backend
must map that cwd and its filesystem paths to the same world if it claims
coherence. Shell-only values are rejected. Pair `DockerBackend` (a shell
transport) with a filesystem transport in the same execution world using
`RemoteExecutionBackend(shell=..., fs=...)`, or use `LocalExecutionBackend`.
An explicit `filesystem=` must be the world's own `.fs` object.

Permissions and execution backends are separate layers. `ToolRule`, `PathRule`,
and `BashRule` determine whether a tool call may run. If a Bash call is
approved, the configured backend determines the runtime boundary. `DockerBackend`
defaults are: writable workspace mount, Docker default
network, no environment forwarding, and no read-only root filesystem. Opt-in
controls such as `network="none"`, `workspace_mount="ro"`,
`read_only_root=True`, `tmpfs=(...)`, `env={...}`, `forward_env=(...)`, and
`user="1000:1000"` restrict approved Bash commands inside the container.

### ToolRegistry

```python
registry.register(tool)                   # add; raises if name exists → Disposable
registry.replace(tool)                    # swap same-named tool → Disposable
registry.unregister(name)                 # remove by name
registry.select(names={...}, tags={...})  # runtime subset (per-request)
registry.copy()                           # shallow clone
registry.schemas()                        # provider-ready schema list
workspace_tools(exclude=..., extra=...)   # configurable software-workspace preset
empty_tools(*extra)                       # no built-ins + optional extras
```

`register`/`replace` are **reversible effects**: each returns a
`linch.Disposable`. `register` rejects a duplicate name; use `replace` for an
intentional hot swap. Disposing a registration removes exactly that registration
(identity-checked, single-use, and idempotent). Disposing a replacement restores
the previous registration when it still owns the slot; disposing a stale handle
never removes a newer tool. Because teardown may be async, always use
`await disposer.dispose()` (or `await disposer()`). See [kernel.md](./kernel.md).

### Deferred tool search

`ToolSearchPlugin` is the default request-catalog plugin. It defers tools when
the offered catalog exceeds 30 tools and defers skill descriptions when more
than 50 model-invokable skills are present. A tool with a truthy duck-typed
`always_load` attribute stays offered; all other deferred tools are represented
by names in the `tool_search` description, without schemas. `tool_search` accepts
`select:a,b` and plain keyword queries over names and descriptions, returns the
matching schemas or skill descriptions, and loads the selected names for the
next request.

Filtering is attached to request construction through the agent's
`request_catalog` capability. It changes provider offerings only: the normal
offered-tool boundary, validation, hooks, and permission engine still govern
execution. A deferred call that bypasses provider compliance receives an
actionable “search first” tool error. Loaded names are persisted in bounded
`Session.meta["linch.tool_search"]`; durable metadata is the source of truth
because search-result content may be offloaded, transformed by middleware, or
removed from the provider projection during compaction.

The request builder calls `request_catalog.prepare()` before selecting tools so
late MCP registrations and other dynamic catalogs are evaluated against the
current counts. Below both thresholds `prepare()` leaves the registry untouched:
no `tool_search` registration is added and the resolver returns no veto, so the
request remains byte-identical. Once a threshold is crossed, the catalog
registers `tool_search`, filters the request after context selection, and
enables the deferred-call guard; `scheduler.py` does not gain a discovery
branch. The terminal-tool path retains a turn-stopping guard so a deferred
terminal call emits the logged search-first feedback before the next request.

The selected-name set is durable session metadata. The current per-request
candidate/offered/skill-name snapshot is JSON-only session extension state,
mirrored into `checkpoint.extension_state["linch.session"]` by the runtime for
mid-turn replay; the plugin does not implement a checkpointable hook.

### Tool pipeline

Before validation, `agent.context.on("tools/resolve", listener)` registers a
serial veto. The listener receives `(tool_name, raw_input, session)` and returns
an immediate-error reason or `None`. Registrations return a `Disposable`; with
no listeners, resolution uses the original path without dispatching the bus.

`ToolPipeline` is an optional per-Agent extension seam around every tool call:

```text
tools/pre-execute → tools/execute → tools/post-execute
```

Register phases with `on_pre_execute`, `on_execute`, and `on_post_execute`;
each registration returns a `Disposable`. Execute listeners are around-wrappers:
they receive a `ToolExecution` record and must `await next()` exactly once to
run the inner body, or may deliberately short-circuit it. Pre and post listeners
can likewise replace the candidate result. With no listeners the scheduler
keeps the direct execution path. An active pipeline contributes an ordered,
stable `resume_policy_id`/version/config to durable run identity; custom
listeners must provide their own stable `resume_policy_id`.
The pipeline runs after canonical validation and permission evaluation. Pipeline
listeners must not mutate authorization-sensitive `input`, `tool`, or decisions;
put rewrites in `PreToolUse`, which is revalidated and re-authorized.

`Agent` does not install `workspace_tools()` implicitly. Pass the explicit
preset when the authority is intentional and visible.
Project-local filesystem, skills, subagents, and MCP resources are likewise
trust-gated by `FeatureFlags` and are not ambient bare-agent defaults.

## Design rationale

- **Duck-typed protocol, no base class.** There is no `isinstance` check anywhere in
  the core, so a tool is anything with the right attributes/methods — an MCP wrapper, a
  `@tool` function, or a hand-written class all drop in identically. Inheritance would
  couple every tool to an SDK type and complicate the MCP/function adapters.
- **`@tool` for the 90% case, raw protocol for the rest.** Most tools are a plain
  function; the decorator infers the schema and wraps the return value, so the common
  case is one line. Advanced needs (custom validation, `resources()`, timeouts) drop to
  the protocol without a different mental model.
- **`scope` + `parallel` are declared, so the scheduler can reason about safety.** A
  tool stating `read`/`write`/`exec` and parallel-safety lets the scheduler parallelize
  reads and serialize writes by default — concurrency falls out of declarations instead
  of caller discipline.
- **Permissions and execution backend are separate layers.** *Whether* a Bash call may
  run (rules) is orthogonal to *where* it runs (Local vs Docker). Keeping them separate
  means you can sandbox execution without rewriting permission policy, and
  `execution_backend` only swaps an existing `Bash` tool — it never grants shell access
  to a registry that omitted it.
- **`deps` instead of globals.** App state is threaded through `ToolContext.deps`
  (per-agent or per-run), so tools stay pure and testable and multiple agents don't
  share module-level state.

---

Back to the [architecture index](./README.md).
