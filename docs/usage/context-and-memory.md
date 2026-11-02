# Context and memory

Use context builders and file memory to supply retrieved data and remembered facts to runs.

[← Usage guide](./README.md)

Linch keeps request context and durable memory on the same extension seam. A
`ContextBuilder` can add ephemeral RAG or live application data to a provider
request. File memory uses the virtual filesystem: the model reads and writes
facts with the existing filesystem tools, while a small index is loaded once
when a run starts.

---

<a id="sourced-retrieval-and-grounding"></a>

## `RagConnector` and `register_grounding_check`

A `RagConnector` implements async `retrieve(query, *, k, principal=None)` and
returns `RetrievedChunk(id, source, text, score, metadata)` values. Install it
with reversible registrations:

```python
from linch import Agent, Principal, RunOptions, register_grounding_check, register_rag_connector

agent = Agent(...)
run_principal = Principal("acme", "user-42", {"reader"})
retrieval = register_rag_connector(agent, connector, k=5, principal=run_principal)
grounding = register_grounding_check(agent)
session = await agent.session()
events = session.run("question", RunOptions(principal=run_principal))
# Later: await grounding.dispose(); await retrieval.dispose()
```

Only one connector may be active per agent. `k` must be a positive integer.
Chunk ids must be nonempty strings without `]`, carriage returns or newlines.
Each request renders `[doc:<id>]`, source and text into ephemeral system blocks.
Retrieval uses the submitted user query across correction retries and durable
resume; an alignment prompt replaces that query. The active principal from
`RunOptions` is passed through unchanged. If registration supplies `principal`,
it is an expected-identity guard only: a missing or different active principal
fails before connector I/O. The connector and host own access checks and
knowledge scoping.

Grounding requires at least one citation to an id returned by the latest
retrieval, and rejects any unknown cited id. It checks every final text block,
forced finals, and recursively checks string values in terminal-tool inputs.
It verifies citation membership, not whether the cited text supports a claim.
Empty retrieval cannot ground an answer. Retrieval errors fail visibly.

An ungrounded answer receives short correction feedback, then fails after
`agent.reliability.max_correction_turns` corrections. This is a separate per-run
counter from built-in reliability corrections; each mechanism can consume its
own bound. `reliability=None` allows zero grounding corrections. Query, latest retrieved
ids and counter survive checkpoints. `required=False` disables interventions;
without an active connector grounding is a no-op.

Retrieved bodies and chunk metadata stay outside the session log and stored
session messages. Typed `ContextBuildEvent.metadata["rag"]` audits only `ids`
and `sources`, including empty retrieval. Context budget trimming does not
change retrieval membership: a trimmed chunk id remains eligible for citation
until the next retrieval replaces the ids. Grounding interventions emit typed
`VerificationEvent` records without retrieved bodies. Exact-model-input snapshots
may contain retrieved text because they preserve the outgoing request.

For durable runs, connectors should expose stable `resume_policy_id`,
`resume_policy_version` and JSON-safe `resume_policy_config` identifying the
knowledge scope and corpus revision; these participate in resume identity.
See [the in-memory keyword connector](../../examples/rag/keyword_connector.py)
for a dependency-free reference. Disposing registrations or closing the agent
removes their hooks, listener and run state.

---

<a id="per-turn-context-building-rag"></a>

## `ContextBuilder` and `ContextInjectionHook`

A `ContextBuilder` is a duck-typed object with an async `build(turn)` method.
It receives the current messages, `turn.deps`, model, tools, and turn index, and
returns a `ContextBuildResult` containing request-scoped messages or system
blocks. The result never mutates the session log.

```python
from linch import Agent, ContextBuildResult, ContextBudget, ContextInjectionHook, Message, TextBlock


class PolicyContextBuilder:
    async def build(self, turn) -> ContextBuildResult:
        docs = await turn.deps.search(turn.messages[-1])
        if not docs:
            return ContextBuildResult()
        return ContextBuildResult(
            messages=[Message(role="user", content=[TextBlock(text=docs)])],
            budget=ContextBudget(max_tokens=800),
            metadata={"source": "policy"},
        )


agent = Agent(..., hooks=[ContextInjectionHook(PolicyContextBuilder())])
```

`ContextInjectionHook` is the standard wiring point. Context builders run for
each provider request, so changing data is naturally ephemeral and does not
accumulate in `session.provider_view`. Request-scoped blocks are kept outside
the static prompt prefix and are not assumed to be cacheable.

The shared dependency object is available as `turn.deps` to builders and as
`ctx.deps` to tools. Hosts should put request or run-specific clients and
backends there rather than using module globals.

---

<a id="file-memory"></a>

## `FileMemoryContextBuilder`

File memory is the built-in durable memory path in Linch 3.0. It has one public
entry point, `FileMemoryContextBuilder`, which is attached through
`ContextInjectionHook`:

```python
from linch import Agent, ContextInjectionHook, FeatureFlags, FileMemoryContextBuilder

agent = Agent(
    ...,
    features=FeatureFlags(filesystem=True),
    hooks=[ContextInjectionHook(FileMemoryContextBuilder())],
)
```

At most once per run, the builder reads `/memories/MEMORY.md` through the run's
filesystem backend. It keeps the first 200 lines and reports truncation when
the index is longer. A missing index still injects the fixed instruction so the
model knows where memory belongs:

> Memories live under `/memories/`, one file per fact. `MEMORY.md` is a
> one-line-per-file index. Use the file tools to read and maintain memories;
> update or delete stale facts.

The index is cached by run, so it stays stable across turns and can remain in a
cacheable request prefix. The model updates memory with `write_file` and
`edit_file` through the ordinary filesystem tools, removing stale entries from
the index as it edits it. Hosts that need to remove a fact file can call the
backend's `delete` operation. A direct host backend call is outside the model
tool path, so it is not automatically serialized, permission-checked, or
logged; use the existing file tools when those guarantees are required.

### Per-run isolation

Hosts create a scoped backend for each run and pass it in `RunOptions(deps=...)`.
`ScopedFileBackend` normalizes every model-supplied path under its root;
absolute paths, `..`, duplicate separators, and odd path segments cannot escape
the scope. Listing returns scope-relative paths.

```python
from linch import RunOptions, ScopedFileBackend, StateFileBackend

inner = StateFileBackend()
tenant_a = ScopedFileBackend(inner, "/tenants/acme/users/alice")
tenant_b = ScopedFileBackend(inner, "/tenants/acme/users/bob")

async for _event in session.run(
    "Remember that I prefer concise updates.",
    opts=RunOptions(deps={"filesystem": tenant_a}),
):
    pass
```

Two concurrent runs can share one `Agent` while using different scoped
backends. Each run sees only its own `/memories/` tree, including through
traversal attempts. `FileMemoryContextBuilder` resolves the backend from the
same `turn.deps` object as the file tools, so reads and writes cannot silently
use a different tenant.

### Stable index shape

Keep `MEMORY.md` short and explicit. Each line points to one fact file, for
example:

```text
/memories/favorite-language.md
/memories/meeting-timezone.md
```

Fact files contain the text the model should remember. When a fact changes,
edit its file and keep the index line. When it is no longer true, delete the
fact file and its index line. The 200-line bound applies to the index; the
filesystem backend owns fact-file size and persistence policy.

---

## Optional semantic recall with mem0

Semantic recall is an example recipe, not a core store or extra. The
`examples/memory/mem0_recall.py` recipe uses only Linch's public context and
hook APIs. Its context builder searches a mem0 `AsyncMemory`-like client with
the latest user text and `filters={"user_id": ...}` from `run.deps`; its
terminal-turn hook calls `add(...)` after the run. A fake client with matching
async `search` and `add` methods is enough to test the recipe without
installing mem0.

Use file memory for explicit, inspectable facts and opt into mem0 when semantic
similarity is worth an external LLM-backed write on each `add` call. Keep the
mem0 client and its user identity in run dependencies so one tenant cannot
search another tenant's memories.

---

## Related pages

- [Hooks](./hooks.md) — `ContextInjectionHook` and hook lifecycle
- [Tools](./tools.md) — filesystem tools and the shared `deps` object
- [Virtual filesystem](./filesystem.md) — `FileBackend`, scoping, and offloading
- [Architecture](../architecture/README.md) — subsystem contracts and invariants
