# Virtual filesystem and large-result offloading

[← Usage guide](./README.md)

Variable-length tool results — RAG passages, web search dumps, large file
reads — are the single biggest cause of context-window blowup. Linch's virtual
filesystem subsystem solves this automatically: oversized tool results are
written to a `FileBackend` and replaced in the conversation with a short preview
plus a path the model can read back on demand.

---

## Why offloading exists

When a tool returns a large payload, the scheduler writes the full result to a
`FileBackend` and replaces what the model sees in `provider_view` with a short
preview and a path reference. The model pulls back only the slice it needs via
`read_file`, instead of carrying the entire dump in every subsequent provider
call. The full original content is never lost — it stays in the backend and on
`ToolCallEndEvent.tool_result` for observers; only the copy that enters
conversation history is trimmed. For how this hooks into the tool execution
chokepoint, see [./tools.md](./tools.md).

---

## On by default

Every `Agent()` enables offloading with an ephemeral `StateFileBackend`. The
threshold is derived automatically from the model's context window
(`threshold_fraction=0.1` → 10 % of the context window). A 128 k-token model
offloads results above ~12 800 tokens; a 200 k model above ~20 000 tokens. No
configuration is required unless you want to change the backend or tune the
threshold.

```python
# Default — ephemeral in-memory backend, threshold = 10 % of context window
agent = Agent(...)   # offload is already on

# Persist offloaded files under .linch/offload (inspectable, gitignored)
from linch.filesystem import DiskFileBackend, OffloadConfig
agent = Agent(
    ...,
    filesystem=DiskFileBackend(root=".linch/offload"),
)

# Tune the threshold or fraction explicitly
agent = Agent(
    ...,
    result_offload=OffloadConfig(threshold_tokens=5_000),   # hard override
    # or:
    result_offload=OffloadConfig(threshold_fraction=0.05),  # 5 % of context
)

# Ephemeral scratch + persistent /memories/ across sessions
from linch.filesystem import CompositeFileBackend, SqliteFileBackend, StateFileBackend
agent = Agent(
    ...,
    filesystem=CompositeFileBackend(
        default=StateFileBackend(),
        routes={"/memories/": SqliteFileBackend(".linch/memories.db")},
    ),
)

# Disable entirely
agent = Agent(..., result_offload=None)
# or: features=FeatureFlags(filesystem=False)
```

### Choosing a backend

The four backends differ only in where they persist and how they scope files:

- **`StateFileBackend`** (default) — in-memory and per-session. Zero setup, but
  files vanish when the session ends. Right for ordinary offloading where the
  model only needs to read payloads back within the same run.
- **`DiskFileBackend(root=...)`** — real files under a sandboxed root (default
  `.linch/offload`, gitignored). Persistent and inspectable on disk, so you can
  open offloaded payloads in your editor while debugging.
- **`SqliteFileBackend(path)`** — persistent across sessions in a SQLite file.
  Use it when the model should read back files from earlier runs.
- **`CompositeFileBackend(default=..., routes=...)`** — routes paths by prefix to
  different backends. The common pattern is ephemeral scratch on `StateFileBackend`
  with a durable `/memories/` partition on `SqliteFileBackend`, so distilled notes
  survive while bulk offloads stay cheap. The deep-agent preset wires exactly this
  `/memories/` partition for you — see [./deep-agent.md](./deep-agent.md).

`DiskFileBackend` and `SqliteFileBackend` perform their I/O off the event loop on
a bounded daemon thread, so persistence never blocks the agent loop.

---

## Auto-registered tools

When the subsystem is active, four tools are registered automatically:

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
also give the model a durable scratchpad for intermediate work that would
otherwise bloat the conversation.

---

## `OffloadConfig` options

```python
OffloadConfig(
    enabled=True,               # master switch
    threshold_tokens=None,      # None = derive from context window (recommended)
    threshold_fraction=0.1,     # fraction used when threshold_tokens is None (10 %)
    preview_lines=10,
    path_prefix="/offload",     # virtual directory for auto-offloaded files
    skip_tools=frozenset({"read_file", "write_file", "edit_file", "ls"}),
)
```

`threshold_tokens` is resolved once at `Agent.__init__` time from
`int(context_window * threshold_fraction)`. Pass an explicit integer to override
(e.g. `threshold_tokens=5_000`). The filesystem tools listed in `skip_tools` are
always excluded from offloading, so reading a large file back does not
recursively re-offload it. Lower `threshold_fraction` (or set a small
`threshold_tokens`) when you want aggressive trimming on a tight context window;
raise it when most results are small and you'd rather avoid the read-back
round-trip.

Set `result_offload=None` or `features=FeatureFlags(filesystem=False)` to turn
the subsystem off entirely — when unset it carries zero overhead until a result
actually exceeds the threshold.

---

## Related pages

- [./tools.md](./tools.md) — automatic offloading of large tool results
- [./deep-agent.md](./deep-agent.md) — the persistent `/memories/` partition
- [./context-and-memory.md](./context-and-memory.md) — keeping the context window lean
- [../architecture.md](../architecture.md) — where the filesystem sits in the loop
