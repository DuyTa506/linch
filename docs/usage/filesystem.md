# Virtual filesystem and large-result offloading

Use the virtual filesystem to offload large tool results and expose file operations.

[← Usage guide](./README.md)

Large tool results — RAG passages, web search dumps, large file reads — can
consume context quickly. When enabled, Linch's virtual filesystem can write
eligible oversized results to a `FileBackend` and replace them in the
conversation with a short preview plus a path the model can read back on demand.

---

## Why offloading exists

When an eligible tool returns an oversized string result, the scheduler stores
the full result (see [where offloaded results are stored](#where-offloaded-results-are-stored))
and replaces what the model sees in
`provider_view` with a short preview and a path reference. Eligibility requires
a successful (non-error) result whose content is a string, whose tool is not in
`skip_tools`, and whose estimated size exceeds the configured threshold. The
model pulls back only the slice it needs via `read_file`, instead of carrying the
entire dump in every subsequent provider call. After a successful offload, the
full original content stays in storage and on `ToolCallEndEvent.tool_result`
for event consumers; only the model-facing copy is trimmed. If the write
fails, the original result remains inline and unchanged. For how this hooks into
the tool execution chokepoint, see [./tools.md](./tools.md).

Bash and MCP results already marked as truncated with a `stdout_path`,
`stderr_path`, or `output_path` in their metadata are not offloaded again. Their
existing spill remains the read-back source; automatic offloading does not copy
it into the virtual filesystem. Shell spill files belong to the managed run;
its cleanup removes them. If cleanup fails, retry session close. See [shell output policy](./extending.md#local-shell-environment-and-output).

---

## Explicit opt-in

Filesystem tools and large-result offloading are opt-in. With the default
`FeatureFlags`, an agent has no virtual filesystem tools or offload backend.
Enable the subsystem explicitly with `FeatureFlags(filesystem=True)` and choose
a backend, or let active sessions use an agent-created `StateFileBackend`.
Supplying an explicit `filesystem=` backend (or an execution world's `fs`)
selects that backend.

When active, the default threshold is derived automatically from the model's
context window (`threshold_fraction=0.1` → 10 % of the context window). A 128
k-token model offloads results above ~12 800 tokens; a 200 k model above ~20
000 tokens.

```python
from linch import (
    Agent,
    CompositeFileBackend,
    DiskFileBackend,
    FeatureFlags,
    OffloadConfig,
    StateFileBackend,
)

# Explicit — ephemeral session backend, threshold = 10 % of context window
agent = Agent(..., features=FeatureFlags(filesystem=True))

# Keep model-written files under .linch/offload (inspectable, gitignored)
agent = Agent(
    ...,
    features=FeatureFlags(filesystem=True),
    filesystem=DiskFileBackend(root=".linch/offload"),
)

# Tune the threshold or fraction explicitly
agent = Agent(
    ...,
    features=FeatureFlags(filesystem=True),
    result_offload=OffloadConfig(threshold_tokens=5_000),   # hard override
    # Or replace the value above with OffloadConfig(threshold_fraction=0.05)
    # for 5 % of context.
)

# In-memory scratch + persistent /memories/ across sessions
import os
from linch.filesystem.postgres import PostgresFileBackend
# Install the optional driver first: pip install 'linch[postgres]'
agent = Agent(
    ...,
    features=FeatureFlags(filesystem=True),
    filesystem=CompositeFileBackend(
        default=StateFileBackend(),
        routes={"/memories/": PostgresFileBackend(os.environ["LINCH_POSTGRES_DSN"])},
    ),
)

# Disable automatic offloading but keep explicit filesystem tools
agent = Agent(
    ...,
    features=FeatureFlags(filesystem=True),
    filesystem=StateFileBackend(),
    result_offload=None,
)

# Disable the entire filesystem subsystem
agent = Agent(..., features=FeatureFlags(filesystem=False))
```

### Choosing a backend

The storage backends below differ in where they persist and how they scope files:

- **`StateFileBackend`** (agent-created active-session default) — in-memory and
  per-session. Zero setup; files are in-memory, not persisted, and a new session
  receives a fresh default backend. Right for scratch notes the model only needs
  within the same run.
- **`DiskFileBackend(root=...)`** — real files under a sandboxed root (default
  `.linch/offload`, gitignored). Persistent and inspectable on disk, so you can
  open the files the model wrote in your editor while debugging.
- **`PostgresFileBackend(dsn)`** — persistent across sessions in Postgres.
  Use it when the model should read back files from earlier runs.
- **`CompositeFileBackend(default=..., routes=...)`** — routes paths by prefix to
  different backends. The common pattern is in-memory scratch on `StateFileBackend`
  with a durable `/memories/` partition on `PostgresFileBackend`, so distilled notes
  survive while scratch files stay cheap. Supply this
  `/memories/` partition explicitly to the deep-agent preset — see [./deep-agent.md](./deep-agent.md).

When no backend is supplied, the agent creates a fresh `StateFileBackend` for
each session. Any explicit backend instance, including the `CompositeFileBackend`
example above and its `StateFileBackend` default, is shared across sessions by
design.

`DiskFileBackend` runs file I/O on a bounded daemon thread; constructing it
resolves and creates its root directory. `PostgresFileBackend` uses asyncpg and
imports it when constructed; install the `linch[postgres]` extra first, while
pool and schema work remain lazy. Both keep backend I/O off the agent loop
thread. Automatic offloading performs no write until an eligible result exceeds
the threshold.

### Where offloaded results are stored

Offloaded results are kept in the session store, under the root session of the
agent tree, whichever filesystem backend is configured: the agent-created
`StateFileBackend`, an execution world's `fs` (the user's workspace), an explicit
`Agent(filesystem=...)`, or `RunOptions(deps={"filesystem": ...})`. They never
appear in a workspace, subagents share them with their parent, and they follow
the session: an agent in another process that opens the same session id from the
same store (worker takeover) still reads them back. `InMemorySessionStore` and
`PostgresSessionStore` support this; a session store without the
[optional capability](./run-stores.md#optional-offloaded-results) keeps every
result inline instead. `read_file`, `ls`, `write_file`, and `edit_file` reach
paths under `path_prefix` (default `/offload`) in the session store; other paths
use the filesystem backend. Offloaded results are deleted with their session and
are not copied by `fork_session`.

---

## Auto-registered tools

When the subsystem is active, four tools are registered automatically. A bare
agent does not receive these tools:

| Tool | Description |
|---|---|
| `ls(prefix?)` | List files in the virtual filesystem |
| `read_file(path, offset?, limit?)` | Read a file, optionally windowed by line range |
| `write_file(path, content)` | Write a scratchpad note or intermediate result |
| `edit_file(path, old_string, new_string, replace_all?)` | Edit an existing file |

The model is informed about these tools and the offload convention via a
system-prompt block added automatically, so you don't have to teach it the
read-back pattern. `read_file`'s `offset`/`limit` line windowing matters here:
the model can read just the relevant slice of a huge offloaded payload rather
than pulling the whole thing back into context. `write_file` and `edit_file`
also give the model a backend-scoped scratchpad for intermediate work that
would otherwise bloat the conversation.

---

## Workspace `Read`, `Write`, and `Edit`

The coding preset's uppercase workspace tools operate on local files under
`ctx.cwd`; they are distinct from the virtual `read_file`, `write_file`, and
`edit_file` tools above. Workspace paths that escape `cwd` are rejected.

`ReadTool(max_read_bytes=10 * 1024 * 1024)` reads UTF-8 text, rejects binary files,
and checks the whole file's size before reading it. Files over the default
10 MiB cap fail with a suggestion to use `Grep`. The optional one-based `offset`
and `limit` select returned lines (default 2,000); they do not bypass the byte
cap or make the disk read windowed. A negative `limit` returns all remaining
lines; zero uses the default. File I/O for these three tools runs off the event
loop.

When `ToolContext.file_read_tracker` is present, overwriting an existing file
with `Write` requires a prior whole-file read and a current `(mtime_ns, size)`
stamp. A partial read is insufficient; creating a new file needs no prior read.
`Edit` requires a current stamp and read coverage for each match being changed.
An external change, including a modification through Bash, returns an error
asking for another `Read`. Successful writes and edits update the stamp; edits
invalidate windowed read coverage. The stamp is a freshness check, not a lock
against concurrent writers.

Read stamps and whole-file read evidence survive durable resume. Partial line
coverage does not; missing stamps require another read. A file changed since
its last read is rejected after resume too.

---

## `OffloadConfig` options

```python
from linch import OffloadConfig

OffloadConfig(
    enabled=True,               # master switch
    threshold_tokens=None,      # None = derive from context window (recommended)
    threshold_fraction=0.1,     # fraction used when threshold_tokens is None (10 %)
    preview_lines=10,
    path_prefix="/offload",     # virtual directory for auto-offloaded files
    skip_tools=frozenset({"read_file", "write_file", "edit_file", "ls"}),
)
```

When `threshold_tokens` is `None`, the agent derives it as
`max(1_000, int(context_window * threshold_fraction))`. The derived value is
refreshed after a successful `provider.prepare()` and after the provider is
replaced. If `context_window()` cannot be resolved, automatic offloading is
skipped unless you supply an explicit integer threshold (for example,
`threshold_tokens=5_000`). The default `skip_tools` is replaceable: it is a
`frozenset` containing the filesystem tool names, but you can pass another
`frozenset` to change which tool results are skipped. Lower
`threshold_fraction` (or set a small `threshold_tokens`) when you want aggressive
trimming on a tight context window; raise it when most results are small and
you'd rather avoid the read-back round-trip.

Set `result_offload=None` to turn off automatic offloading. This does not disable
filesystem tools when `features=FeatureFlags(filesystem=True)` and an explicit
`filesystem` backend (or execution-world `fs`) is present. Set
`features=FeatureFlags(filesystem=False)` to disable the entire subsystem. With
the default flags it is already inactive; enabling it is an explicit trust
decision.

---

## Related pages

- [./tools.md](./tools.md) — automatic offloading of large tool results
- [./deep-agent.md](./deep-agent.md) — the persistent `/memories/` partition
- [./context-and-memory.md](./context-and-memory.md) — keeping the context window lean
- [../architecture/subsystems.md#39-virtual-filesystem-and-result-offload](../architecture/subsystems.md#39-virtual-filesystem-and-result-offload) — where the filesystem sits in the loop
