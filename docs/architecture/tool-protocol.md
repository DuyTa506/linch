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
    parallel: bool                                 # V2 concurrency flag

    # Optional Phase-11 reliability attributes (all duck-typed via getattr)
    execution_timeout_ms: float                    # per-tool timeout; 0 = opt-out
    retryable: bool                                # opt write/exec tool into retry

    def validate(self, raw: dict) -> dict: ...
    def resources(self, input: dict) -> list[ResourceAccess]: ...
    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult: ...
    def summarize(self, input: dict) -> str: ...   # one-line for logs
```

`ToolContext` carries: `cwd`, `session_id`, `run_id`, `session_store`, `signal` (abort), `file_read_tracker`, `deps`, `filesystem`.

`deps` is threaded from `Agent(deps=...)` or overridden per-run with `RunOptions(deps=...)`. Use it to inject app state into tools without globals.

`BashTool` delegates command execution to a backend. `LocalBackend` preserves
the default local subprocess behavior with timeout cleanup; `DockerBackend`
uses `docker run --rm` when the Docker daemon is available. Passing
`Agent(execution_backend=...)` replaces an existing `Bash` tool only, so a
restricted registry that omits `Bash` does not gain shell access.

Permissions and execution backends are separate layers. `ToolRule`, `PathRule`,
and `BashRule` determine whether a tool call may run. If a Bash call is
approved, the configured backend determines the runtime boundary. `DockerBackend`
defaults remain compatibility-first: writable workspace mount, Docker default
network, no environment forwarding, and no read-only root filesystem. Opt-in
controls such as `network="none"`, `workspace_mount="ro"`,
`read_only_root=True`, `tmpfs=(...)`, `env={...}`, `forward_env=(...)`, and
`user="1000:1000"` restrict approved Bash commands inside the container.

### ToolRegistry

```python
registry.add(tool)                        # add; raises if name exists
registry.remove(name)                     # remove by name
registry.replace(tool)                    # swap same-named tool
registry.select(names={...}, tags={...})  # runtime subset (per-request)
registry.copy()                           # shallow clone
registry.schemas()                        # provider-ready schema list
empty_tools(*extra)                       # no built-ins + optional extras
tools_from_defaults(exclude, extra)       # standard set ± named tools
```

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
