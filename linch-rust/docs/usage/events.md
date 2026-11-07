# Events

Use this page to read a run's event stream: every event type the client
decodes, its fields, and how sequence numbers and replay work.

[<- Usage guide](./README.md)

Iterating a `Run` yields `Envelope`s. Each holds one event, decoded into a
frozen dataclass from `linch_rust`. Nested values (messages, usage, tool
results) stay as the JSON dicts the server sends; the child event a
`subagent` event wraps is decoded like any other. Decoding is tolerant: a
field this client does not know is dropped, and a type it has no class for
becomes `UnknownEvent`.

---

## Render a stream

`match` on the event classes:

```python
import asyncio

from linch_rust import (
    AgentConfig,
    AssistantEvent,
    ContextBuildEvent,
    Envelope,
    ErrorEvent,
    InitEvent,
    PartialAssistantEvent,
    ResultEvent,
    Server,
    SuspendedEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    UnknownEvent,
    UsageEvent,
    UserEvent,
    tool,
)
from linch_rust.scripted import text_turn, tool_turn


@tool(scope="read")
async def weather(city: str) -> str:
    """Current weather for a city."""
    return f"{city}: 18C, light rain"


def render(envelope: Envelope) -> str | None:
    match envelope.event:
        case InitEvent(model=model, tools=tools):
            return f"start {model} with {tools}"
        case UserEvent(subtype="prompt", message=message):
            return f"user: {message['content'][0]['text']}"
        case ContextBuildEvent(messages=messages):
            return f"calling the model with {messages} messages"
        case PartialAssistantEvent(delta={"kind": "text", "text": text}):
            return f"  ...{text}"
        case ToolCallStartEvent(tool_name=name, summary=summary):
            return f"tool {name}: {summary}"
        case ToolCallEndEvent() as end:
            state = "failed" if end.is_error else "ok"
            return f"tool {end.tool_name} {state} in {end.duration_ms} ms"
        case AssistantEvent(stop_reason=reason):
            return f"assistant turn ({reason})"
        case UsageEvent(cumulative=total, cumulative_cost_usd=cost):
            return f"tokens so far {total['input_tokens']} in / {total['output_tokens']} out, ${cost}"
        case ErrorEvent(error=error):
            return f"error: {error['message']}"
        case SuspendedEvent(pending_request_ids=pending):
            return f"waiting for approval of {pending}"
        case ResultEvent(subtype=subtype, final_text=text):
            return f"{subtype}: {text}"
        case UnknownEvent(type=kind):
            return f"({kind})"
        case _:
            return None


config: AgentConfig = {
    "namespace": "events-demo",
    "spec": {"model": "claude-sonnet-4-6"},
    "provider": {
        "id": "scripted",
        "turns": [
            tool_turn("c1", "weather", {"city": "Hanoi"}, model="claude-sonnet-4-6"),
            text_turn("Rainy, 18C.", model="claude-sonnet-4-6"),
        ],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[weather])
        async for envelope in await agent.run("s1", "Weather in Hanoi?"):
            line = render(envelope)
            if line is not None:
                print(f"{envelope.seq:>3} {line}")


asyncio.run(main())
```

```text
  1 start claude-sonnet-4-6 with ['weather']
  2 user: Weather in Hanoi?
  3 calling the model with 1 messages
  6 assistant turn (tool_use)
  7 tokens so far 3 in / 1 out, $2.4e-05
  8 tool weather: weather
  9 tool weather ok in 0 ms
 11 calling the model with 3 messages
 12   ...Rainy, 18C.
 14 assistant turn (end_turn)
 15 tokens so far 6 in / 2 out, $4.8e-05
 16 success: Rainy, 18C.
```

Each class also has a `type` class attribute (`"init"`, `"tool_call_end"`,
...) if you prefer to compare strings.

## The envelope

| Field | What it is |
|---|---|
| `seq` | The run's durable sequence number. A stream-only event carries the seq of the last durable record before it |
| `run_id` | The run the event belongs to |
| `ts_ms` | When the server made the envelope, in Unix milliseconds |
| `event` | The decoded event |

### Sequence numbers

Seqs grow through the run but are not contiguous from your side: the run also
writes accounting records for each model call (the dispatch and its receipt)
that take seqs and are never sent to clients. In the stream above, 4 and 5 are
the first call's records and the `partial_assistant` at 12 carries seq 12, the
second call's dispatch record.

Replay with `await agent.events(run_id, after_seq=n)`: it returns the run's
durable envelopes with a seq above `n`, in order, from the stores. It never
returns stream-only events. So a client that saw seq `n` and lost its stream
reads exactly what it missed.

## Durable and stream-only

| Kind | Stored and replayed |
|---|---|
| `partial_assistant`, `tool_progress`, `hook` | No: sent at once, never replayed |
| Every other kind | Yes: sent once it is durable |

A run's stream ends with `result` (finished) or `suspended` (waiting for
`approve`).

## Event types

### `InitEvent` (`init`)

The run started.

| Field | What it is |
|---|---|
| `session_id`, `run_id` | The session and run |
| `model` | `spec.model` |
| `provider` | The provider's id: `anthropic`, `openai-chat`, `scripted`, ... |
| `tools` | Names of the tools offered, in order |
| `parent_run_id` | The parent run, for a subagent's run; `None` otherwise |
| `trace_id` | The `trace_id` given to `agent.run`, kept for the whole run and its subagents' runs; `None` when none was given ([Observability](./observability.md#correlate-logs-and-traces)) |
| `resumed` | `True` on the `init` that starts a resume or approve of the run, with the run's current tools; `False` on the run's first |
| `turn` | The turn the call continues in: the run checkpoint's, for a resumed `init`; `0` on the run's first |

Every `run`, `resume` and `approve` stream starts with `init`. A resumed one
is durable, so replayed events show each call's start.

### `UserEvent` (`user`)

A user-role message entered the conversation.

| Field | What it is |
|---|---|
| `message` | `{"role": "user", "content": [blocks]}` |
| `subtype` | `prompt` (your prompt), `tool_result` (the results of a tool batch), `notification` (a runtime notice, such as a background worker finishing) or `alignment` |

### `AssistantEvent` (`assistant`)

A complete model response.

| Field | What it is |
|---|---|
| `message` | `{"role": "assistant", "content": [blocks]}`: `text`, `thinking`, `tool_use` blocks |
| `stop_reason` | `end_turn`, `tool_use`, `max_tokens`, `stop_sequence`, `refusal`, `error`, `lease_lost` or `interrupted` |

### `PartialAssistantEvent` (`partial_assistant`), stream-only

A piece of the response as it streams. `delta` is one of:

| `delta["kind"]` | Other keys |
|---|---|
| `text` | `text` |
| `thinking` | `text` |
| `tool_use_input` | `tool_use_id`, `json_delta` (a piece of the call's JSON input) |

The complete response follows as an `AssistantEvent`, so you can ignore deltas
if you do not stream text to a user.

### `ContextBuildEvent` (`context_build`)

The request for the next model call was assembled; the call starts.

| Field | What it is |
|---|---|
| `model` | The model the call requests: `spec.model`, or a loaded skill's `model` for the turn the skill narrows |
| `system_blocks` | Number of system blocks |
| `messages` | Number of conversation messages sent |
| `selected_tools` | Tool names offered in this request |
| `budget` | Context accounting; `{}`, as the server trims no context |
| `metadata` | What the `context_build` callbacks returned as `metadata`, merged, a later callback's key winning; `{}` without any |

### `UsageEvent` (`usage`)

Token usage of one model call. See [Usage and cost](#usage-and-cost).

| Field | What it is |
|---|---|
| `usage` | This call: `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens` |
| `cumulative` | The run so far, same keys |
| `cost_usd` | This call's cost in USD; `None` when the model has no price |
| `cumulative_cost_usd` | The run's priced calls so far; `None` until one is priced |
| `actual_model` | The model the provider reported serving this call (it can differ from the one asked for, for example a dated id); `None` when it named none |

### `ToolCallStartEvent` (`tool_call_start`)

A tool call is about to run. With Postgres stores it is durable before the
tool starts.

| Field | What it is |
|---|---|
| `tool_use_id` | The call's id, from the model |
| `tool_name` | The tool |
| `input` | The input, after validation and any `pre_tool_use` rewrite |
| `summary` | One line for logs and approval prompts |

### `ToolCallEndEvent` (`tool_call_end`)

A tool call finished.

| Field or property | What it is |
|---|---|
| `tool_name` | The tool |
| `result` | The tool result block the model reads: `{"tool_use_id", "content": [blocks], "is_error"?}` |
| `duration_ms` | How long the call took |
| `canonical_output` | The structured value, for tools that declare an output schema; `None` otherwise |
| `metadata` | Details for the host only, never shown to the model: what a host tool returned in [`ToolOutput.metadata`](./tools.md#metadata-for-the-host), or where an offloaded result went; `None` for an error or a tool that set none |
| `error_code` | A failed call's machine-readable code (`invalid_input`, `not_found`, ...), when its tool gave one; `None` otherwise |
| `tool_use_id` | Property: `result["tool_use_id"]` |
| `is_error` | Property: whether the result is a tool error |

A refused, denied or blocked call also ends with a `tool_call_end` whose
`is_error` is true. A call interrupted by a crash gets no `tool_call_end`;
its placeholder result is in the next `user` event of subtype `tool_result`.

### `ToolProgressEvent` (`tool_progress`), stream-only

Progress from a running tool: `tool_use_id`, `tool_name`, `message`, and
`data` (a dict or `None`). No built-in tool reports progress yet, and host
tools have no way to.

### `PermissionRequestEvent` (`permission_request`)

Calls that need your approval. `requests` is a list of
`{"tool_use_id", "tool_name", "input", "summary"}`. A `suspended` event
follows. See [Tools](./tools.md#approvals).

### `SuspendedEvent` (`suspended`)

The run stopped to wait; its stream ends here.

| Field | What it is |
|---|---|
| `status` | `awaiting_approval` |
| `pending_request_ids` | The `tool_use_id`s to answer with `agent.approve` |

### `CompactionEvent` (`compaction`)

The older part of the conversation was summarized
([`spec.compaction`](./agent.md#compaction)): `messages_before`,
`messages_after`, `tokens_before`, `tokens_after` (estimates), `strategy`,
and `duration_ms`, the time from the decision to compact to this event (the
summary call and the projection).

### `LoopGuardEvent` (`loop_guard`)

The run hit its turn limit: `reason` (`max_turns`), `detail` (for example
`Maximum turns (20) reached.`) and `action` (`stop`). An `error` event and a
`result` of subtype `error` follow.

### `BudgetEvent` (`budget`)

The run's [budget](./agent.md#budget) crossed its warning ratio (`kind`
`warning`, once per run and its subagents) or stopped the run (`exceeded`,
followed by an `error` event and a `result` of subtype `error`). It carries
`spent_tokens` and `spent_usd` across the tree, and the caps, `max_tokens` and
`max_cost_usd` (`None` when not set).

### `HookEvent` (`hook`), stream-only

One [callback](./agent.md#callbacks) call and how it ended.

| Field | What it is |
|---|---|
| `point`, `policy_id`, `policy_version` | Which callback |
| `tool_use_id` | The call it was about, at the tool points; else `None` |
| `outcome` | `{"status": "answered", "verdict"}`, `{"status": "failed", "error", "fail_mode"}` or `{"status": "timed_out", "timeout_ms", "fail_mode"}` |

### `ErrorEvent` (`error`)

An error the run recorded. `error` is a dict with `name` and `message`, and
`retryable`; a provider failure also has `kind` (`transport`, `status`,
`invalid_request`, `stream`) and, for an HTTP failure, `status`:

```text
{'name': 'ProviderError', 'kind': 'status', 'message': 'provider returned HTTP 503: busy', 'retryable': True, 'status': 503}
{'name': 'TurnLimitError', 'message': 'max turns exceeded', 'retryable': False}
{'name': 'BudgetExceededError', 'message': 'run budget exhausted (30 tokens, $0.0400 spent)', 'retryable': False}
```

A `result` of subtype `error` follows.

### `ResultEvent` (`result`)

The run finished; its stream ends here.

| Field | What it is |
|---|---|
| `subtype` | `success`, `error`, `aborted` (cancelled) or `interrupted` |
| `stop_reason` | Why the loop stopped, as in `AssistantEvent` |
| `final_text` | The last answer's text; `None` when there is none |
| `total_usage` | Usage summed over every call whose usage is known |
| `usage_complete` | `False` when some call's usage was unknown and is not in the sum |
| `duration_ms` | Wall time of the `run`, `resume` or `approve` call that ended the run. The whole run's span is this envelope's `ts_ms` minus the first `init` envelope's (the one not `resumed`) |
| `total_cost_usd` | Cost of the run's priced calls; `None` when none was priced |
| `structured_output`, `structured_error` | For runs with an output schema; `None` with this client |
| `trace_id` | The run's `trace_id`, as on `InitEvent` |

### `SkillsLoadedEvent` (`skills_loaded`)

The skills the model may load, on a session's first run of an agent with
skills, between `init` and the prompt's `user` event. `skills` holds one
dict per skill, the built-in `verify` included, sorted by name: `name`,
`description`, and `when_to_use` and `argument_hint` when the skill sets
them. Python Linch sends it once per session object, so again after a
restart; here a session's later runs never do.

### `SkillInvokedEvent` (`skill_invoked`)

The `Skill` tool loaded a skill. `name` is the skill and `args` the argument
text it was given, or `None`. `model_override` and `allowed_tools` are
always `None` today. See [Skills](./skills.md).

### `SkillCompletedEvent` (`skill_completed`)

That skill's call ended: `name`, and `is_error` (`True` when the skill could
not be loaded).

### `SubagentEvent` (`subagent`)

An event of a subagent's child run.

| Field | What it is |
|---|---|
| `child_run_id` | The child run: `<parent run id>:<tool_use_id>` of the call that runs it |
| `tool_use_id` | The parent's call that runs the child: the `Subagent` call, or the `SubagentContinue` call that continues a worker |
| `ts_ms` | When the child emitted the event, from the child's own envelope. A background child's events reach the parent's log at its next turn, so the outer envelope's `ts_ms` is later |
| `event` | The child's event, decoded into its own class (an `InitEvent`, a `ToolCallEndEvent`, another `SubagentEvent` for a grandchild, ...) |

A foreground subagent's events, its stream-only ones included, reach the
parent's stream this way; a background worker's durable ones do. See
[Subagents](./subagents.md).

### `ChildRunStatusEvent` (`child_run_status`)

A background worker's child run changed status: `child_run_id` and `status`
(`running`, then `completed`, `failed` or `aborted`).

### Kinds the server does not emit yet

The core declares these kinds and the client decodes each into its class,
but no run emits them today:

| Class (`type`) | Fields |
|---|---|
| `InputRequestEvent` (`input_request`) | `request_id`, `kind` (`ask_user` or `workflow_interrupt`), `prompt` |
| `PromptCacheAdvisoryEvent` (`prompt_cache_advisory`) | `reason` (`tool_set_changed` or `model_changed`), `detail` |
| `VerificationEvent` (`verification`) | `verifier`, `action` (`retry`, `stop` or `exhausted`), `feedback`, `attempt` |
| `WorkflowEvent` (`workflow`) | `kind`, `title`, `call_key`, `occurrence`, `subagent_type`, `result_text`, `structured_output`, `structured_error` |

### `UnknownEvent`

A type this client has no class for, from a newer server. `type` is the
wire type and `raw` the payload without it.

## Usage and cost

`usage` counts four separate buckets: `input_tokens` is the uncached input
only, apart from `cache_read_tokens` and `cache_creation_tokens`. Each
adapter subtracts what its vendor folds into the prompt count.

Cost fields are filled from a built-in price table matched on the exact model
id of the call. It holds Anthropic models only (for example
`claude-sonnet-4-6`, `claude-opus-4-8`, `claude-haiku-4-5`); every other model
reports `None`. With several models in one run the totals add only the priced
calls. Pricing is not part of the run contract. The rates are in the
[architecture providers page](../architecture/providers.md#pricing).

For spans and metrics built from these events, see
[Observability](./observability.md).

## Related pages

- [Runs](./runs.md): resume, approve, cancel, durability.
- [Tools](./tools.md): what produces `tool_call_*` and `permission_request`.
- [Architecture](../architecture/README.md): the Rust side.
