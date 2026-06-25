# Context and memory

[← Usage guide](./README.md)

Linch gives you two complementary layers for getting the right information in
front of the model: a **per-turn context builder** for ephemeral RAG and memory
snippets, and a set of **memory primitives** (stores, search tools, and a
tiered router) for durable recall. Both are wired through the same dependency
object and stay out of your conversation history unless you put them there.

---

## Per-turn context building (RAG)

A `ContextBuilder` runs once before every provider call. Its job is to look at
the turn-so-far and return *ephemeral* context — RAG passages, recalled
memories, request-scoped system blocks — that is appended to the provider
request **only for that call**. It never mutates `session.provider_view`, so
nothing it injects becomes permanent conversation history. This is exactly what
you want for retrieval: the freshest, most relevant snippets each turn, with no
unbounded accumulation.

A builder is any object with an async `build(turn) -> ContextBuildResult`
method (it is a duck-typed protocol, no base class to inherit):

```python
from linch.context import ContextBudget, ContextBuildResult
from linch.types import Message, TextBlock

TAG = "[[ctx]]"

class MyContextBuilder:
    async def build(self, turn) -> ContextBuildResult:
        docs = await turn.deps.search(last_query(turn.messages))
        if not docs:
            return ContextBuildResult()
        return ContextBuildResult(
            messages=[
                Message(role="user", content=[TextBlock(text=f"{TAG}\n{docs}")])
            ],
            budget=ContextBudget(max_tokens=800),
            metadata={"source": "my_store"},
        )

from linch.hooks import ContextInjectionHook

agent = Agent(..., hooks=[ContextInjectionHook(MyContextBuilder())], deps=my_store)
```

The `turn` passed to `build` carries everything you need to decide what to
inject: `turn.messages` (the current `provider_view`), `turn.deps` (your shared
dependency object — see below), `turn.model`, `turn.tools`, and `turn.turn_index`.

`ContextBuildResult` is the return contract. Populate only the fields you need:

- `messages` — ephemeral `Message`s appended to the provider request for this turn.
- `system_blocks` — request-scoped system prompt additions.
- `selected_tools` — a per-turn tool override (request-scoped tool selection).
- `budget` — a `ContextBudget(max_tokens=...)` that caps how much room this
  context may consume; the framework trims to fit and records what it did.
- `metadata` — an arbitrary dict surfaced on the emitted `ContextBuildEvent`,
  handy for tracing which store or strategy produced the context.

Returning an empty `ContextBuildResult()` (as the early-return above does) is the
right move when there is nothing relevant — you skip the injection entirely
rather than pad the request.

### Wiring via a hook

Context builders are attached through the **hooks** layer, not a dedicated
constructor argument. Wrap your builder in `ContextInjectionHook` and pass it in
`Agent(hooks=[...])`:

```python
from linch.hooks import ContextInjectionHook

agent = Agent(..., hooks=[ContextInjectionHook(MyContextBuilder())], deps=my_store)
```

`ContextInjectionHook` builds the `turn` from session state, calls your
builder, and applies the `ContextBudget` for you before the result is appended.
For the full hook lifecycle — including how to stack `ContextInjectionHook`
alongside other adapters like `ToolMiddlewareHook` and `RunTelemetryHook` — see
[./hooks.md](./hooks.md).

### The shared `deps` object

The object you pass as `Agent(deps=...)` is handed to **both** your context
builder (as `turn.deps`) and your tools (via `ToolContext`). That means a single
tenant-specific store, request-scoped service bundle, or per-user client can
drive context selection and tool execution from one place — no globals, no
duplicate wiring.

```python
async for event in session.run("Find policy citations for PTO rollover."):
    if event.type == "context_build":
        print("context metadata:", event.metadata)
    elif event.type == "tool_call_start":
        print("tool:", event.summary)
    elif event.type == "result":
        print(event.final_text)
```

The `context_build` event exposes the `metadata` you set on the result, so you
can observe exactly what each turn pulled in. See [./tools.md](./tools.md) for how
the same `deps` reaches tool `execute()` calls, and [./events.md](./events.md) for
the full event reference.

---

## Memory and RAG primitives

For durable recall that survives across turns and sessions, Linch ships memory
stores, a memory-backed context builder, and search/upsert tools. These are
deliberately dependency-free in core: vector databases and embedding models stay
in your host app or an adapter that implements the `MemoryStore` protocol.

```python
from linch import Agent
from linch.memory import (
    InMemoryKeywordMemoryStore,
    MemoryContextBuilder,
    MemoryItem,
    MemorySearchTool,
    TieredMemoryStore,
)
from linch.tools.registry import empty_tools

store = InMemoryKeywordMemoryStore()
await store.upsert([
    MemoryItem(id="m1", content="ToolResult can carry citations.", namespace="docs")
])

agent = Agent(
    ...,
    deps=store,
    hooks=[ContextInjectionHook(MemoryContextBuilder(namespace="docs", max_tokens=800))],
    tools=empty_tools(MemorySearchTool(namespace="docs")),
)
```

Here every piece composes through the standard seams:

- `InMemoryKeywordMemoryStore` is a cooperative, dependency-free keyword store —
  perfect for tests and small deployments. Swap it for `SqliteMemoryStore`,
  `PostgresMemoryStore`, or your own embedding-backed adapter without changing the
  rest of the wiring.
- `MemoryItem` is the unit of storage: an `id`, `content`, a `namespace` for
  scoping, and an optional `metadata` dict.
- `MemoryContextBuilder` is a ready-made context builder that recalls relevant
  items each turn and injects them ephemerally. Wire it through
  `ContextInjectionHook` exactly like any custom builder, and bound it with
  `max_tokens` so recall never crowds out the conversation.
- `MemorySearchTool` (and its sibling `MemoryUpsertTool`) let the **model**
  query or write memory on demand, rather than relying solely on the builder's
  automatic recall.

### Tiered memory

For long-running, multi-session, user-oriented agents, wrap stores with
`TieredMemoryStore` so working, episodic, and semantic memories are routed and
ranked separately:

```python
working = InMemoryKeywordMemoryStore()
episodic = InMemoryKeywordMemoryStore()
semantic = InMemoryKeywordMemoryStore()
store = TieredMemoryStore(working=working, episodic=episodic, semantic=semantic)

await store.upsert([
    MemoryItem(
        id="pref-1",
        content="The user prefers concise status updates.",
        namespace="user:42",
        metadata={"tier": "semantic"},
    )
])
```

`TieredMemoryStore` is itself a `MemoryStore`, so it drops into the same `deps`
slot. Writes route by `item.metadata["tier"]` (defaulting to `working` when
unset), and searches fan out across all three sub-stores and merge the results.
This keeps short-lived task notes (`working`), event history (`episodic`), and
distilled long-term facts (`semantic`) from contaminating each other's ranking.

### Optional Postgres memory

Core includes `MemoryStore` protocols, cooperative in-memory keyword memory,
SQLite memory, optional Postgres memory via `pip install 'linch[postgres]'`,
tiered memory, and memory search/upsert tools. `PostgresMemoryStore` mirrors the
SQLite store's keyword-search API, so you can promote from SQLite to Postgres for
shared, persistent memory without touching your builder or tool wiring. Vector
databases and embedding models stay in the host app or an adapter. See
[Vector memory adapters](./vector-memory-adapters.md) for FAISS, pgvector, and
Qdrant recipes using this same `MemoryStore` shape.

---

## Keep the layers separate (production guidance)

For production long-running agents, keep the layers separate so each one does the
job it is good at:

- Use a `ContextBuilder` for ephemeral RAG and memory snippets that should not
  be persisted into conversation history.
- Use `TieredMemoryStore` metadata to separate working task notes, episodic
  events, and distilled semantic facts.
- Let virtual filesystem offloading replace large tool payloads with durable
  references the model can read back on demand.
- Keep `RunStore` enabled for checkpoints, durable approvals, resume, and
  `load_run_report()` diagnostics.

The payoff is a context window that stays lean: recall is fresh and bounded each
turn, durable facts live in tiered memory, and bulky payloads live on the virtual
filesystem — see [./filesystem.md](./filesystem.md) — instead of inflating the
provider request.

---

## Related pages

- [./hooks.md](./hooks.md) — `ContextInjectionHook` and the hook lifecycle
- [./tools.md](./tools.md) — the shared `deps` object and `ToolContext`
- [./vector-memory-adapters.md](./vector-memory-adapters.md) — vector DB adapter recipes
- [./filesystem.md](./filesystem.md) — offloading large payloads to durable references
- [../architecture.md](../architecture.md) — how context and memory fit the loop
