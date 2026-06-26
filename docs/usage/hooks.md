# Hooks

[← Usage guide](./README.md)

Hooks are the **canonical extension mechanism** for the Linch runtime. A hook is
a plain Python object with one or more `on_*` methods; you register hooks with
`Agent(hooks=[...])` and the loop calls them at typed chokepoints. Through hooks
you can observe a run (telemetry), transform data in flight (rewrite prompts,
tool input/output, the final answer), or control the loop (block a tool, stop
early, force another turn).

Hooks replace the older `observers=`, `middleware=`, `context_builder=`,
`verifiers=`, and `RunOptions(stop_when=...)` parameters. Each of those is now a
built-in **adapter** you wrap in a hook (see [Built-in adapters](#built-in-adapters)).

```python
from linch import Agent, HookResult

class GuardTools:
    def on_pre_tool_use(self, ctx):
        if ctx.tool_name == "Bash" and "rm -rf" in str(ctx.input.get("command", "")):
            return HookResult.block("blocked dangerous command")
        return None

agent = Agent(model="gpt-5", hooks=[GuardTools()], permissions={"mode": "skip-dangerous"})
```

A hook method receives a typed *context* and returns either `None` (do nothing)
or a `HookResult`. Methods may be **sync or async**. An exception raised inside a
hook is caught by the dispatcher, recorded as a `hook` telemetry event with
`action="error"`, and the loop continues — one faulty hook never crashes a run.

---

## Chokepoints

Each chokepoint has an event name, an `on_*` method, and a context object. A
hook only needs to define the methods it cares about.

| Method | Fires | Context fields | Can control |
|---|---|---|---|
| `on_agent_start` | run begins | `model`, `prompt`, `tools` | observe only |
| `on_user_prompt_submit` | before the prompt is appended | `prompt`, `images` | mutate prompt/images, block/stop |
| `on_turn_start` / `on_turn_stop` | around each turn | `turn_index` | observe only |
| `on_before_provider_call` | before each provider call | `request`, `context_result` | mutate request, block/stop, force_continue |
| `on_provider_call_start` / `on_provider_call_stop` | around the provider call | `model`, `stop_reason`, `usage`, `duration_ms` | observe only |
| `on_after_provider_call` | after the assistant turn is assembled | `assembly` | mutate assembly, block/stop, retry/force_continue |
| `on_pre_tool_use` | after permission, before execution | `tool_name`, `input`, `tool` | mutate input, block/stop |
| `on_tool_use_start` / `on_tool_use_stop` | around each tool | `tool_name`, `result`, `is_error`, `duration_ms` | observe only |
| `on_post_tool_use` | after a tool result is produced | `tool_name`, `input`, `result` | mutate result, block/stop |
| `on_before_final_answer` | before a text/structured final answer | `final_text`, `structured_output`, `structured_error`, `stop_reason` | mutate answer, block/stop, retry/force_continue |
| `on_stop` | the run is about to return its `ResultEvent` | `result_event` | mutate result, force_continue, stop |
| `on_subagent_start` / `on_subagent_stop` | around a subagent run | `subagent_type`, `display_name`, `prompt`, `result` | observe only |
| `on_event_emit` | for every event yielded to the caller | `event` | observe only |

A single object may implement any subset of these. To handle *every* event in
one place, define `on_hook(event_value, ctx)` instead of the per-event methods
— if present, `on_hook` is used for all events and the per-event methods are
ignored.

---

## `HookResult`

Return `None` to do nothing, or a `HookResult` to act. Use the factory methods:

```python
HookResult.continue_()                       # explicit no-op
HookResult.mutate(prompt="...", input={...}) # replace one or more fields
HookResult.block("reason")                   # reject (tool/prompt) → error result
HookResult.retry("feedback")                 # bounce the answer back for another turn
HookResult.force_continue("feedback")        # run another turn even past a stop
HookResult.stop("reason")                    # end the run with an error result
HookResult.resolve(tool_result=tr)           # PreToolUse: serve tr, skip execution
```

Which actions are honored depends on the chokepoint:

- **`mutate`** — only the fields meaningful to that chokepoint are read:
  `prompt`/`images` (UserPromptSubmit), `request` (BeforeProviderCall),
  `assembly` (AfterProviderCall), `input` (PreToolUse), `tool_result`
  (PostToolUse), `final_text`/`structured_output`/`structured_error`
  (BeforeFinalAnswer), `result_event` (Stop). When several hooks mutate the same
  context, the changes are threaded through in order — the next hook sees the
  previous hook's value.
- **`block` / `stop`** — at `PreToolUse`/`PostToolUse` the tool result becomes an
  error; at `UserPromptSubmit`/`BeforeProviderCall`/`AfterProviderCall`/
  `BeforeFinalAnswer`/`Stop` the run ends with an error `ResultEvent` (the
  `on_agent_stop` / `on_run_end` lifecycle still fires).
- **`retry` / `force_continue`** — at `BeforeFinalAnswer` and `Stop` they bounce
  the would-be answer back into the loop with the feedback injected as a
  follow-up message, costing one turn (so they're bounded by `max_turns` and the
  run [budget](./agent.md#run-budgets)). `AfterProviderCall` also honors them.
  A `BeforeProviderCall` `force_continue` re-runs the turn without calling the
  provider.
- A graceful early **success** stop is available at `BeforeProviderCall`: return
  `HookResult.stop("stop_when", metadata={"subtype": "success"})` (this is how
  `StopPredicateHook` works).
- **`resolve`** — only at `PreToolUse`: the tool is **not executed**; the supplied
  `tool_result` becomes the outcome (success or error per its `is_error`). This is
  how a cache serves a hit — see [`ToolCacheHook`](./tool-cache.md).

Lifecycle-only chokepoints (`agent_start/stop`, `turn_*`, `provider_call_*`,
`tool_use_*`, `subagent_*`, `event_emit`) ignore the return value — use them for
telemetry.

### Tool-pairing note

When a hook bounces a **structured-output final-tool** answer back into the loop
(schema repair, a `retry`, or a `force_continue`), the runtime answers the
pending terminal `tool_use` with a synthetic `tool_result` carrying the feedback,
so the next provider request stays well-formed (providers reject a `tool_use`
with no matching `tool_result`). You don't have to do anything for this.

---

## Built-in adapters

Common extension patterns ship as adapters. Import them from `linch.hooks` (the
core ones are also re-exported from `linch`).

| Adapter | Wraps | Replaces |
|---|---|---|
| `ContextInjectionHook(builder)` | one or more `ContextBuilder`s | `context_builder=` |
| `ToolMiddlewareHook(middleware)` | tool middleware (`before_tool_call` / `after_tool_result`) | `middleware=` |
| `FinalAnswerVerifierHook(verifiers, max_retries=2)` | output verifiers | `verifiers=` / `max_verification_retries=` |
| `StopPredicateHook(predicate)` | a `(session) -> bool` stop predicate | `RunOptions(stop_when=...)` |
| `RunTelemetryHook(observers)` | one or more `RunObserver`s | `observers=` |
| `ToolCacheHook(config)` | per-run memoization of read-scope tool calls | `tool_cache=` — see [tool-cache.md](./tool-cache.md) |
| `ReadBeforeWriteHook(config)` | read-before-edit gate for the virtual filesystem | `read_before_write=` (default `True`) |

```python
from linch.hooks import (
    ContextInjectionHook,
    FinalAnswerVerifierHook,
    RunTelemetryHook,
    StopPredicateHook,
    ToolMiddlewareHook,
)

agent = Agent(
    ...,
    hooks=[
        ContextInjectionHook(MyRagBuilder()),          # per-turn RAG → context-and-memory.md
        ToolMiddlewareHook(MyGovernanceMiddleware()),  # transform/redact tool I/O
        FinalAnswerVerifierHook(MyVerifier(), max_retries=2),
        StopPredicateHook(lambda s: saw_enough(s)),
        RunTelemetryHook([LoggingObserver()]),         # → OTel, Langfuse, etc.
    ],
)
```

Notes:

- **`ContextInjectionHook`** exposes `build_context()` and feeds the per-turn
  context pipeline. With multiple separate context hooks their outputs are merged
  and then re-budgeted as a whole. See [context-and-memory.md](./context-and-memory.md).
- **`ToolMiddlewareHook`** is **fail-closed**: if your middleware raises in
  `before_tool_call` the tool is *blocked* (error result), and if it raises in
  `after_tool_result` the result becomes an error — a guard that throws never
  silently lets the tool through.
- **`ReadBeforeWriteHook`** (installed by default, disable with
  `Agent(read_before_write=False)`) blocks an in-place `edit_file` on the virtual
  filesystem until that file has been read or written this session — a successful
  `read_file`/`write_file` marks it. Workspace `Edit` is *not* covered here: the
  builtin `Edit` tool enforces its own read-before-edit gate (single source of
  truth, keeps its "You must Read…" message), so this flag governs only the
  virtual gate. Whole-file overwrites (`Write`/`write_file`) are allowed by
  default — that is their purpose — but a host can opt into overwrite-gating of
  *existing* files via `ReadBeforeWriteConfig(overwrite_tools=...)`. A windowed
  (`offset`/`limit`) read does not unlock edits to the unseen parts of a file.
- **`RunTelemetryHook`** translates loop events into the `RunObserver` protocol
  (`on_run_start/end`, `on_turn_*`, `on_provider_call_*`, `on_tool_*`,
  `on_event`). It also forwards `aclose()`/`close()` to the wrapped observers on
  `Agent.close()`, so exporters get flushed. Vendor backends (Langfuse,
  LangSmith, Honeycomb, Datadog) are reached through the OpenTelemetry observer.

---

## Telemetry: `HookEventRecord`

Every hook invocation that **acts** emits a `hook` event in the stream
(`HookEventRecord`) with `event` (the chokepoint), `hook` (the hook's `name` or
class name),
`action` (`mutate`/`block`/`retry`/`stop`/`force_continue`/`resolve`/`error`),
and `reason`. A hook that returns `None` (a no-op `continue`) emits nothing, so a
default-on hook that fires on every tool call does not flood the stream. This
makes hook decisions visible to UIs, [run reports](./events.md), and `on_event`
consumers without extra wiring.

```python
from linch import is_hook_event

async for event in session.run("..."):
    if is_hook_event(event):
        print(event.hook, event.event, event.action, event.reason)
```

---

## Cleanup

Hooks that hold resources can expose `close()` or `aclose()`; `Agent.close()`
calls them (after stores and the filesystem) and swallows errors so shutdown is
never blocked. `RunTelemetryHook` uses this to flush wrapped observers.

---

## Related pages

- [Context & memory](./context-and-memory.md) — `ContextInjectionHook` in depth.
- [Tools](./tools.md) — what `PreToolUse`/`PostToolUse` see, and permissions.
- [Structured output](./structured-output.md) — schema repair and final-tool capture.
- [Events](./events.md) — the event stream `on_event_emit` mirrors.
