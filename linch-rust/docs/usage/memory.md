# Memory

[Tiếng Việt](../vi/usage/memory.md)

Use this page to keep a long run inside the model's context window, to
recover when it does not fit or an answer is cut off, and to give an agent a
memory that outlives its sessions.

An agent remembers in three places:

| What | Where it lives | Set up with |
|---|---|---|
| The conversation of a session | The session's log, in its store; the model sees a view of it | Nothing: every session has it |
| A shorter view of a long conversation | A summary (or cleared tool results) logged in the session | [`spec.compaction`](#compaction) |
| Facts the agent keeps in its own files | `/memories/MEMORY.md` in the virtual filesystem | [`filesystem.memory`](#file-memory) |
| Facts kept across sessions | Your own store: files, a vector index, Notion, a database | A [`context_build` callback](#external-memory) to read it, a host tool to write it |

Two more spec fields recover a model call that went wrong because of size:
[a prompt longer than the context window](#when-the-prompt-does-not-fit)
and [an answer the output limit cut off](#cut-off-answers).

---

## Compaction

A run compacts at the start of a turn, before it calls the model, when the
view it is about to send is due. It folds the older part of the view into one
summary and keeps the last turns as they were. The session keeps every
message; only what the model sees gets shorter.

```python
from linch_rust import AgentConfig

config: AgentConfig = {
    "namespace": "support",
    "spec": {
        "model": "claude-sonnet-4-6",
        "compaction": {"keep_recent_turns": 6, "micro": True},
    },
    "provider": {"id": "anthropic"},
}
```

`"compaction": {}` turns it on with every default. Absent, a run never
compacts. The fields, typed as `Compaction`:

| Field | Default | What it controls |
|---|---|---|
| `threshold_tokens` | none: follow the context window | Compact a view estimated at this many tokens or more (about four characters per token, 1000 per image). Without it, compact when the estimate plus an output reserve reaches 80% of the provider's [context window](./providers.md#context-window) |
| `keep_recent_turns` | `10` | Assistant turns kept verbatim after the summary; at least 1 |
| `prompt` | the built-in prompt | Instructions for the summary call. `DETAILED_COMPACTION_PROMPT` is Python Linch's `DetailedCompaction` (below) |
| `max_output_tokens` | `4096` | Most tokens the summary may use; at least 1 |
| `micro` | `False` | Clear old tool results before summarizing ([below](#micro-compaction)) |
| `max_forced` | `3` | Most summaries one run makes because the provider refused a prompt as too long ([below](#when-the-prompt-does-not-fit)); `0` never |

A summary is one extra provider call on the agent's provider and model, with
its own usage, charged to the [budget](./agent.md#budget). It is reported by
a `compaction` [event](./events.md#compactionevent-compaction) with
`strategy` `summary`. The `pre_compact` and `post_compact` callbacks see it.

The compaction spec is part of the [run contract](./agent.md#the-run-contract):
a run started under one setting is not resumed under another.

### What the model sees after it

```text
system blocks and tools      unchanged, byte for byte
first message                unchanged
<summary of earlier conversation> ... (a user message)
the last keep_recent_turns assistant turns, and what follows them
```

The system blocks, the tools and the first message do not change, so the
provider's cached prefix up to them survives a compaction. With skills on,
the summary also restates each skill the model loaded; with a tool catalog
that changed during the session, it restates the changes.

A workspace agent also forgets what the model read: after a compaction the
model must `Read` a file again before it can `Edit` or `Write` it, since the
content it read may have been folded away (Python Linch's
`reset_read_tracker`).

### Micro-compaction

With `micro`, a due view is first cleared, with no model call: every tool
result before the kept turns is replaced by `[tool result elided to save
context]`, unless it is already no longer than that and holds no image. Each
tool call keeps its result, so the view stays valid for every provider. If
the cleared view is no longer due, the turn goes on with it and no summary
is made (`strategy` `micro`). If it is still due, the summary is made from
the cleared view.

Clearing changes messages in the middle of the view, so the cached prefix
ends at the first cleared result, where a summary would end it at the
summary. Leave `micro` off when the cache matters more than a summary call.

### The detailed prompt

Python Linch's `DetailedCompaction` is a summary prompt in six numbered
sections (request, key artifacts, errors and fixes, pending tasks, current
work, next step), written so the work can go on from the summary alone. It
ships as `DETAILED_COMPACTION_PROMPT`:

```python
from linch_rust import DETAILED_COMPACTION_PROMPT

spec = {
    "model": "claude-sonnet-4-6",
    "compaction": {"prompt": DETAILED_COMPACTION_PROMPT, "max_output_tokens": 8192},
}
```

---

## When the prompt does not fit

Each provider refuses a prompt longer than the model's context window in its
own words. The adapter recognizes that refusal (the table below) and the run
reads it as `context_length`, not as an ordinary failure: another identical
call cannot succeed, so it is never [retried](./providers.md#failures-and-retries).

With `spec.compaction` set, the run shortens its view and calls again, in
this order:

1. With `micro`, it clears the old tool results, if any are worth clearing.
2. Else it makes a summary, keeping `keep_recent_turns` (or only the last
   turn, when that is what does not fit). Each one counts against
   `max_forced`.
3. When neither can shorten the view (nothing is left to fold, or
   `max_forced` is spent), or the summary call fails, the run ends.

Each step is a `compaction` event with `forced` true. The call that follows
is the same turn's next attempt, not a provider retry, and no
`provider_retry` event is sent for it. A refusal from a call that cost
nothing is not charged.

Without `spec.compaction`, or once the steps run out, the run ends with an
`error` event like this one and a `result` of subtype `error`:

```text
{'name': 'ProviderError', 'kind': 'context_length', 'message': "prompt exceeds the model's context window: prompt is too long: 201838 tokens > 200000 maximum", 'retryable': False, 'status': 400}
```

Python Linch always has a compaction strategy to fall back on. linch-rust
compacts only when the spec asks, so set `compaction` on any agent whose
runs may grow past the window.

### What each provider's refusal looks like

| Provider | Recognized as `context_length` |
|---|---|
| `anthropic` | HTTP 400 whose message says `prompt is too long` or `exceed context limit`. A 413 (the request body is too large in bytes) stays an HTTP failure |
| `openai-chat` | HTTP 400 with code `context_length_exceeded`, or a message that says `maximum context length` |
| `openai-responses` | HTTP 400, or a `response.failed` event, with code `context_length_exceeded`, or a message that says `exceeds the context window` or `maximum context length` |
| `gemini` | HTTP 400 whose message says `exceeds the maximum number of tokens allowed` |
| `deepseek` | HTTP 400 whose message says `maximum context length` |
| `vllm` | HTTP 400 whose message says `maximum context length` |
| `sglang` | HTTP 400 whose message says `maximum context length` or `exceeds the maximum allowed length` |
| `llamacpp` | HTTP 400 with type `exceed_context_size_error`, or a message that says `exceeds the available context size` or `larger than the max context size` |

Message matching ignores case. The Anthropic, Gemini and DeepSeek messages are not
documented by their vendors; they are the ones those APIs send today. A
provider that changes its wording makes the refusal an ordinary HTTP 400
again, which ends the run as before.

To test your handling offline, a [scripted](./providers.md#the-scripted-provider)
turn can fail with `{"error": {"type": "context_length", "status": 400,
"message": "..."}}`.

---

## Cut-off answers

A model that reaches its output limit stops mid-answer, with stop reason
`max_tokens`. `spec.truncation_recovery` (typed as `TruncationRecovery`,
Python Linch's `TruncationRecovery`) sends such an answer back:

```python
spec = {
    "model": "claude-sonnet-4-6",
    "max_output_tokens": 2048,
    "truncation_recovery": {"max_attempts": 2},
}
```

| Field | Default | What it controls |
|---|---|---|
| `max_attempts` | `1` | Most cut answers one run continues; at least 1. Past it, a cut answer is final |
| `feedback` | "Your previous response was cut off because it reached the output token limit. Continue exactly where you left off — do not repeat earlier text." | What the model is told, in a `<system-reminder>`; not blank |

The cut part is logged as the model's answer, the feedback is logged as the
next user message (a `user` event with subtype `notification`), and the
model takes another turn. The run's final answer is every part in order,
and that whole text is what the [output schema](./structured-output.md),
the [verifiers](./security.md#verifiers) and `stop` callbacks see. Only an
answer with text and no tool call is continued; a turn that calls tools
starts a new answer. Each continuation is a turn, so `max_turns` bounds it.

---

## File memory

`filesystem: {"memory": true}` gives a plain agent the memory a
[deep agent](./deep-agent.md) has: the model keeps notes in the
[virtual filesystem](./tools.md#virtual-filesystem) under `/memories/`, one
file per fact, with `/memories/MEMORY.md` as a one-line-per-file index. It
needs no preset; it is Python Linch's `FileMemoryContextBuilder`.

```python
config = {
    "namespace": "support",
    "spec": {"model": "deepseek-flash"},
    "provider": {"id": "deepseek"},
    "stores": {"type": "postgres", "dsn": "postgres://..."},
    "filesystem": {"memory": True},
}
```

Before each model call the server adds two text blocks to that request: an
instruction (memories live under `/memories/`, `MEMORY.md` is the index, use
the file tools to keep them and drop stale facts) and, when the file exists
and is not empty, its first 200 lines (a note says when it is longer). The
blocks ride the tail of the request, like [external memory](#external-memory),
and are never logged. Nothing writes memory for the model: it uses
`write_file` and `edit_file`, so the usual permissions apply.

- **Read for each request, not once per run.** Python Linch reads the index
  once per run and keeps it, so the prompt does not change mid-run. Here the
  blocks are never part of the cached prefix, which stays the same either
  way, and the tail of the last turn is not logged, so it cannot be cached
  across requests. Reading again costs one file read and lets the model see
  an index it edited on its next turn. A run resumed in another process
  reads the file again in both designs.
- **Where the file lives.** With Postgres stores every session of the
  namespace shares `/memories/`, so session 2 sees what session 1 wrote, and
  so does a new process. With memory stores each session has its own files,
  kept in the agent handle: nothing is shared between sessions, and
  `close_session` drops them, so the file does not outlive its session. Use
  Postgres stores for memory that lasts.
- **The contract.** Off, it is not sent and the run contract is unchanged;
  on, the hook is part of the contract, so a run cannot resume across turning
  it on or off. A `deep_agent` always has it, whatever `memory` says.
- `memory` is a field of `filesystem`, so it needs the filesystem on; there
  is no way to ask for it with the filesystem off.

---

## External memory

Long-term memory is a store you own. The agent reads it through a
`context_build` [callback](./agent.md#callbacks): before each model
call, the callback searches the store and returns text blocks. The blocks
ride only that one request, at its tail, as part of the last user message.
They are never logged, so:

- the session does not grow with retrieved text, and a compaction never has
  to fold it;
- the cached prefix of every later request stays the same;
- the next request searches again, with the conversation as it is then.

`linch_rust.memory.retrieval` builds that callback from any object with a
`retrieve` method:

```python
from linch_rust import Server
from linch_rust.memory import Chunk, retrieval


class Wiki:
    async def retrieve(self, query: str, *, k: int) -> list[Chunk]:
        hits = await search_my_index(query, limit=k)  # your vector index, Notion, SQL...
        return [Chunk(id=h.id, source=h.url, text=h.body, score=h.score) for h in hits]


async with Server() as server:
    agent = await server.open_agent(config, callbacks=[retrieval(Wiki(), k=4)])
```

| `retrieval` argument | Default | What it controls |
|---|---|---|
| `retriever` | required | The store; `retrieve(query, *, k)` returns `Chunk`s, best first, or a coroutine of them |
| `k` | `4` | Most chunks per request |
| `policy_id`, `policy_version` | `"memory"`, `1` | The callback's place in the run contract; bump the version when the store's behaviour changes |
| `timeout` | none | Seconds the server waits; on a failure or a timeout the request goes without memory (`context_build` fails open) |

The query is the latest message the user wrote, skipping tool results and
`<system-reminder>` feedback, so the question that started a run's tool calls
stays the query through them. Each chunk reaches the model as:

```text
[doc:{id}]
Source: {source}
{text}
```

The ids and sources land on the call's `ContextBuildEvent` as
`metadata["memory"]`, for observers and evals; the model never sees the
metadata.

Every request of a `context_build` callback has `session_id` and `run_id`.
To keep one user's memory apart from another's, write your own callback that
picks the store from them; `latest_question` and `render` in
`linch_rust.memory` are the pieces `retrieval` is made of.

### Writing memories

The model writes memory by calling a host tool you declare, so you decide
what a memory is and where it goes:

```python
from linch_rust import tool


@tool(scope="write")
def remember(title: str, text: str) -> str:
    """Save a fact for later conversations, as a short titled note."""
    return store.remember(title, text)
```

A `write` tool asks for approval in the `default` permission mode; run the
agent in `accept_edits`, or allow the tool by a permission rule.

`python/examples/external_memory.py`, in the repository (it does not ship in the wheel), puts both halves together on a folder
of Markdown notes, like an Obsidian vault: a keyword `NoteStore`, `retrieval`
over it, and a `remember` tool. It runs offline with the scripted provider,
and `tests/test_memory.py` runs it, checking that a note saved in one run is
recalled in the next and that no recalled text is in the session.

### Why a callback and not a connector type

Python Linch has `RagConnector`, a protocol the context builder calls, and
`RetrievedChunk`. Here, the store runs in your process and the run runs in
the server, so a connector would be one more callback type with the same
round trip as `context_build`. `Retriever` and `Chunk` have the connector's
shape, and `retrieval` is the adapter, in the client, where the store's
library lives. Python Linch puts retrieved text in system blocks; here it
rides the tail of the request, so retrieval never moves the cached prefix.

---

## Compared with Python Linch

| Python Linch | linch-rust |
|---|---|
| `CompactionLadder` with a micro rung and forced summaries | `spec.compaction` with `micro` and `max_forced` |
| `DetailedCompaction` | `spec.compaction.prompt = DETAILED_COMPACTION_PROMPT` |
| A custom compaction strategy | Not supported: `prompt` and `max_output_tokens` are the knobs |
| Recovery from a prompt-too-long error, always on | Only with `spec.compaction`; without it the refusal ends the run |
| `reset_read_tracker` after a compaction | Built in for workspace agents |
| `TruncationRecovery` | `spec.truncation_recovery` |
| `RagConnector`, `ContextBuilder`, `FileMemoryContextBuilder` | A `context_build` callback; `linch_rust.memory.retrieval` |

## Related pages

- [Agent config](./agent.md#compaction): every `spec` field.
- [Events](./events.md#compactionevent-compaction): the `compaction` event.
- [Providers](./providers.md#failures-and-retries): what is retried.
- [Architecture: compaction](../architecture/compaction.md): the barriers and
  the recovery of each step.
