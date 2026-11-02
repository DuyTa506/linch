# Hooks

Use hooks to observe or change agent behavior at typed lifecycle chokepoints.

[← Usage guide](./README.md)

Hooks are the **canonical extension mechanism** for the Linch runtime. A hook is
a plain Python object with one or more `on_*` methods; you register hooks with
`Agent(hooks=[...])` and the loop calls them at typed chokepoints. Through hooks
you can observe events, transform data in flight (rewrite prompts, tool
input/output, the final answer), or control the loop (block a tool, stop early,
force another turn). Runtime traces and metrics come from OpenTelemetry at the
I/O source; they do not require a telemetry hook.

Every hook chokepoint is a `hook` point in the generated [lifecycle
catalog](../architecture/events.md#typed-lifecycle-catalog); `HookDispatcher`
awaits hooks in registration order. The separate `tools/pre-execute`,
`tools/execute`, and `tools/post-execute` points are waterfalls for tool
wrappers; user hooks stay on the adapter so mutation composition, failure
isolation, and the security-sensitive `PreToolUse` ordering remain explicit.

```python
from linch import Agent, HookResult, workspace_tools

class GuardTools:
    def on_pre_tool_use(self, ctx):
        if ctx.tool_name == "Bash" and "rm -rf" in str(ctx.input.get("command", "")):
            return HookResult.block("blocked dangerous command")
        return None

agent = Agent(
    model="gpt-5",
    tools=workspace_tools(),
    hooks=[GuardTools()],
    permissions={"mode": "skip-dangerous"},
)
```

A hook method receives a typed *context* and returns either `None` (do nothing)
or a `HookResult`. Methods may be **sync or async**. An exception raised inside a
hook is caught and logged by the dispatcher, which records an error for that
dispatch and continues to the next hook. Some lifecycle points do not forward
those records to the stream. Return `HookResult.block(...)` to reject a tool;
raising an exception does not block it.

---

## Chokepoints

Each chokepoint has an event name, an `on_*` method, and a context object. A
hook only needs to define the methods it cares about.

| Method | Fires | Context fields | Can control |
|---|---|---|---|
| `on_agent_start` / `on_agent_stop` | run begins / ends | `model`, `prompt`, `tools` / `result` | observe only |
| `on_user_prompt_submit` | before the prompt is appended | `prompt`, `images` | mutate prompt/images, block/stop |
| `on_turn_start` / `on_turn_stop` | around each turn | `turn_index` | observe only |
| `on_before_provider_call` | before each provider call | `request`, `context_result` | mutate request, block/stop, force_continue |
| `on_provider_call_start` / `on_provider_call_stop` | around the provider call | `model`, `stop_reason`, `usage`, `duration_ms` | observe only |
| `on_after_provider_call` | after the assistant turn is assembled | `assembly` | mutate assembly, block/stop, retry/force_continue |
| `on_pre_tool_use` | after provider validation, before final permission evaluation | `tool_name`, `input`, `tool` | mutate input, block/stop |
| `on_tool_use_start` / `on_tool_use_stop` | around each tool | `tool_name`, `result`, `is_error`, `duration_ms` | observe only |
| `on_post_tool_use` | after a tool result, including failure | `tool_name`, `input`, `result`, `tool_output` | mutate result, block/stop |
| `on_post_tool_use_failure` | after a failed result | `tool_name`, `input`, `result`, `tool_output` | observe only |
| `on_pre_compact` / `on_post_compact` | around compaction | message and token counts, `strategy` | observe only |
| `on_before_final_answer` | before a text/structured final answer | `final_text`, `structured_output`, `structured_error`, `stop_reason` | mutate answer, block/stop, retry/force_continue |
| `on_stop` | before returning a candidate final `ResultEvent` | `result_event` | mutate result, force_continue, block/stop |
| `on_subagent_start` / `on_subagent_stop` | around a subagent run | `subagent_type`, `display_name`, `prompt`, `result` | observe only |
| `on_event_emit` | for stream events | `event` | observe only |

A single object may implement any subset of these. To handle *every* event in
one place, define `on_hook(event_value, ctx)` instead of the per-event methods
— if present, `on_hook` is used for all events and the per-event methods are
ignored.

For durable runs, a custom policy hook must expose a host-owned
`resume_policy_id` and may expose `resume_policy_version` and JSON-safe
`resume_policy_config`. The same rule applies to policy-bearing adapters such
as `ToolMiddlewareHook`, `ContextInjectionHook`, `FinalAnswerVerifierHook`, and
`StopPredicateHook`, because their wrapped callback can change execution even
though the adapter class is built in. A checkpointable hook's `checkpoint_key`
only names its persisted state slot; it does not prove policy equivalence.

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
  or `tool_output` (PostToolUse), `final_text`/`structured_output`/`structured_error`
  (BeforeFinalAnswer), `result_event` (Stop). When several hooks mutate the same
  context, the changes are threaded through in order — the next hook sees the
  previous hook's value.
- **`block` / `stop`** — at `PreToolUse`/`PostToolUse` the tool result becomes an
  error; at `UserPromptSubmit`/`BeforeProviderCall`/`AfterProviderCall`/
  `BeforeFinalAnswer`/`Stop` the run ends with an error `ResultEvent` (the
  `on_agent_stop` lifecycle still fires).
- **`retry` / `force_continue`** — at `BeforeFinalAnswer` they bounce
  the would-be answer back into the loop with feedback as a follow-up message.
  `AfterProviderCall` also honors both; `Stop` honors `force_continue` only.
  Further turns are bounded by `max_turns` and the run
  [budget](./agent.md#runbudget).
  A `BeforeProviderCall` `force_continue` re-runs the turn without calling the
  provider.
- A graceful early **success** stop is available at `BeforeProviderCall`: return
  `HookResult.stop("stop_when", metadata={"subtype": "success"})` (this is how
  `StopPredicateHook` works).
- **`resolve`** — only at `PreToolUse`: the tool is **not executed**; the supplied
  `tool_result` becomes the outcome (success or error per its `is_error`). This is
  how a cache serves a hit — see [`ToolCacheHook`](./tool-cache.md).

`PreToolUse` is the input-mutation seam. After all such mutations, Linch
validates the canonical input again and evaluates rules and `can_use_tool` on
that final value. Approval callbacks decide whether to allow the final input;
to change it, use `PreToolUse`.

Lifecycle-only chokepoints (`agent_start/stop`, `turn_*`, `provider_call_*`,
`tool_use_*`, `post_tool_use_failure`, `pre_compact`, `post_compact`,
`subagent_*`, `event_emit`) ignore the return value — use them for
telemetry.

### Tool-pairing note

Before the next provider request, the runtime pairs any pending `tool_use` with
a synthetic `tool_result` so the conversation stays well-formed. Repair feedback
is carried by the paired result or a subsequent user message, depending on the
path. You don't have to do anything for this.

---

## Built-in adapters

Common extension patterns ship as adapters. Import them from `linch`.

| Adapter | Wraps |
|---|---|
| `ContextInjectionHook(builder)` | one or more `ContextBuilder`s |
| `ToolMiddlewareHook(middleware)` | tool middleware (`before_tool_call` / `after_tool_result`) |
| `FinalAnswerVerifierHook(verifiers, max_retries=2)` | output verifiers |
| `StopPredicateHook(predicate)` | a `(session) -> bool` stop predicate |
| `ToolCacheHook(config)` | per-run memoization of read-scope tool calls — see [tool-cache.md](./tool-cache.md) |
| `ReadBeforeWriteHook(config)` | read-before-edit gate for the virtual filesystem (opt-in) |
| `RedactionHook(config)` | host-supplied regex scrubbing of tool results, the final answer, and (opt-in) the prompt |

```python
from linch import (
    ContextInjectionHook,
    FinalAnswerVerifierHook,
    StopPredicateHook,
    ToolMiddlewareHook,
)

agent = Agent(
    model="gpt-5",
    hooks=[
        ContextInjectionHook(MyRagBuilder()),          # per-turn RAG → context-and-memory.md
        ToolMiddlewareHook(MyGovernanceMiddleware()),  # transform/redact tool I/O
        FinalAnswerVerifierHook(MyVerifier(), max_retries=2),
        StopPredicateHook(lambda s: saw_enough(s)),
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
  silently lets the tool through. Canonical-only `tool_output` values bypass
  this result adapter.
- **`ReadBeforeWriteHook`** is opt-in (`Agent(read_before_write=True)`) and only
  matters when the virtual filesystem tools are explicitly enabled. It blocks
  an in-place `edit_file` on the virtual filesystem until that file has been
  read or written this session — a successful `read_file`/`write_file` marks it.
  Workspace `Edit` is *not* covered here: the builtin `Edit` tool enforces its
  own read-before-edit gate (single source of truth, keeps its "You must Read…"
  message), so this flag governs only the virtual gate. Whole-file overwrites
  (`Write`/`write_file`) are allowed by default — that is their purpose — but a
  host can opt into overwrite-gating of *existing* files via
  `ReadBeforeWriteConfig(overwrite_tools=...)`. A windowed (`offset`/`limit`)
  read does not unlock edits to the unseen parts of a file. A bare Linch 3.0
  `Agent` has no filesystem tools, no offload backend, and no read-before-write
  hook unless the embedder opts into them.
- **Tool progress is live-only.** `ToolContext.report_progress(...)` is
  emitted as `ToolProgressEvent` and passed to `on_event_emit` when the
  stream advances, but it is not
  added to provider history or the durable run event log.
- **`RedactionHook`** is a **policy-free governance seam**: it applies
  caller-supplied `RedactionRule(pattern, replacement)` regexes to tool-result
  text (`PostToolUse`), the final answer (`BeforeFinalAnswer`), and — opt-in via
  `RedactionConfig(redact_user_prompt=True)` — the user prompt. Linch ships **no
  default patterns**, so the policy (what counts as sensitive) stays with the
  embedder and an empty rule set is a no-op. Canonical-only `tool_output`
  values bypass the tool-result redactor. A bad regex raises at construction,
  not mid-run. See [`examples/core/governance_redaction.py`](../../examples/core/governance_redaction.py).
  For richer, stateful transforms use `ToolMiddlewareHook` instead.

### OpenTelemetry

Linch uses `opentelemetry-api` directly. With no SDK configured, its tracer and
meter calls are no-ops. A host can inject SDK providers into an agent's
`Context`, keeping tenants isolated when several agents run in one process:

```python
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from linch import Agent, Context

host_context = Context()
host_context.register("tracer_provider", TracerProvider())
host_context.register("meter_provider", MeterProvider())
agent = Agent(model="gpt-5", provider=my_provider, context=host_context)
```

Hosts own the SDK providers and their exporter lifecycle. Registering a direct
`tracer` or `meter` in the context is also supported when a host already has
instrumentation objects. The built-in source instrumentation emits this span
tree: `invoke_agent` for each run, `step` for each turn, `chat` for each
provider call, `execute_tool` for each tool, and one span per compaction. It
records GenAI attributes such as `gen_ai.operation.name`,
`gen_ai.provider.name`, `gen_ai.request.model`, finish reasons, usage/cache
tokens, conversation and tool identifiers, plus `linch.run_id` and
`linch.turn_index`. The metrics are `gen_ai.client.token.usage`,
`gen_ai.client.operation.duration`, and `linch.store.duration` (with an `op`
attribute around loop store calls).

Prompt, assistant, and tool content is absent by default. Hosts that have a
deliberate content policy can opt in with the standard environment switch:

```bash
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true
```

The API works with any OTLP exporter. Configure the host's exporter endpoint
for Langfuse, Grafana Tempo, Datadog, or another OTLP collector; the Linch
agent and hook configuration stays the same. See
[`examples/observability/observability_agent.py`](../../examples/observability/observability_agent.py)
for a self-contained console-exporter setup.

To correlate host logs with a run, attach `TraceIdFilter` to a handler. It
prefers an explicit `extra={"trace_id": ...}`, then the active OTel span,
`record.run_id`, and an optional fallback. `ToolContext.trace_id` exposes the
same value to tool implementations as a read-only property; tools can pass it
explicitly with `logger.info("...", extra={"trace_id": ctx.trace_id})`.

---

## Telemetry: `HookEventRecord`

The dispatcher records every returned `HookResult`, including an explicit
`HookResult.continue_()`, as a `HookEventRecord`. Returning `None` emits no
record. The record contains the chokepoint, hook name, action, and reason.
Exceptions and invalid return values produce an `error` record; exceptions are
also logged. Only some chokepoints forward records to the stream, so these
records are not a complete audit log of hook invocations.

```python
from linch import is_hook_event

async for event in session.run("..."):
    if is_hook_event(event):
        print(event.hook, event.event, event.action, event.reason)
```

---

## Cleanup

Hooks that hold resources can expose `close()` or `aclose()`; `Agent.close()`
calls them after sessions, stores, and the filesystem are idle. A close error
leaves the agent quiescing; calling `close()` again retries only unfinished
resources. OpenTelemetry SDK providers are host-owned and should be shut down
by the host after its agents close.

---

## Related pages

- [Context & memory](./context-and-memory.md) — `ContextInjectionHook` in depth.
- [Tools](./tools.md) — what `PreToolUse`/`PostToolUse` see, and permissions.
- [Structured output](./structured-output.md) — schema repair and final-tool capture.
- [Events](./events.md) — the event stream `on_event_emit` mirrors.
