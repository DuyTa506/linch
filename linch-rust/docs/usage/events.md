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

The gaps are by design: a seq the client never sees is not a lost event, so
**a gap alone does not tell you an event was lost**. To check, replay from the
last seq you hold (below) and compare; the stores are the record, the stream is
a live view of it.

Replay with `await agent.events(run_id, after_seq=n)`: it returns the run's
durable envelopes with a seq above `n`, in order, from the stores. It never
returns stream-only events. So a client that saw seq `n` and lost its stream
reads exactly what it missed.

## Durable and stream-only

| Kind | Stored and replayed |
|---|---|
| `partial_assistant`, `tool_progress`, `hook`, `cache_diagnostic` | No: sent at once, never replayed |
| Every other kind | Yes: sent once it is durable |

A run's stream ends with `result` (finished) or `suspended` (waiting for
`approve` or `answer`).

## Event types

### `InitEvent` (`init`)

The run started.

| Field | What it is |
|---|---|
| `session_id`, `run_id` | The session and run |
| `model` | `spec.model` |
| `provider` | The provider's id: `anthropic`, `openai-chat`, `scripted`, ... |
| `tools` | Names of the tools the model is offered, in order: the agent's, then the [final tool](./structured-output.md#the-final-tool) when the run made it from its output schema (a registered `final_tool_name` is already among the agent's) |
| `parent_run_id` | The parent run, for a subagent's run; `None` otherwise |
| `trace_id` | The `trace_id` given to `agent.run`, kept for the whole run and its subagents' runs; `None` when none was given ([Observability](./observability.md#correlate-logs-and-traces)) |
| `resumed` | `True` on the `init` that starts a resume or approve of the run, with the run's current tools; `False` on the run's first |
| `turn` | The turn the call continues in: the run checkpoint's, for a resumed `init`; `0` on the run's first |
| `output_schema` | The run's own [output schema](./structured-output.md#give-one-run-its-own-schema), on the first `init` of a run started with one; `None` otherwise |
| `tool_choice` | The run's own [tool choice](./structured-output.md#choose-which-tool-the-model-calls), on the first `init` of a run started with one; `None` otherwise |
| `final_tool` | The name of the run's [final tool](./structured-output.md#the-final-tool), on the first `init` of a run that has one; `None` otherwise |

Every `run`, `resume` and `approve` stream starts with `init`. A resumed one
is durable, so replayed events show each call's start: a run that was
resumed once holds two `init` events, the first with `resumed=False` and the
resume's with `resumed=True` (and the `turn` it continues in). A tool call a
resume finds interrupted is closed with a `tool_call_end` whose result has
`is_error` set, so a replay of the run reads a start and an end for each call.

### `UserEvent` (`user`)

A user-role message entered the conversation.

| Field | What it is |
|---|---|
| `message` | `{"role": "user", "content": [blocks]}` |
| `subtype` | `prompt` (your prompt), `tool_result` (the results of a tool batch), `notification` (a runtime notice, such as a background worker finishing, or a hook's feedback that takes another turn) or `alignment` (a [steer](./runs.md#steer): text you sent to the run) |
| `steer_id` | On `alignment`: the id `steer` returned. Absent otherwise |

### `AssistantEvent` (`assistant`)

A complete model response.

| Field | What it is |
|---|---|
| `message` | `{"role": "assistant", "content": [blocks]}`: `text`, `thinking`, `tool_use` blocks. A `tool_use` block is `{"type", "id", "name", "input"}`, and carries `raw_input` (the arguments as the model wrote them) only when they were not a JSON object: `input` is then `{}` and the call is answered with an error, never run ([Troubleshooting](./troubleshooting.md)) |
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
| `budget` | The [context budget](./memory.md#context-budget)'s decision for this request: `{}` without `spec.context_budget`; with it `max_tokens`, `used_tokens`, `remaining_tokens`, `trimmed`, `dropped` (a list of `{source, index, tokens}`, ordered by callback and `index`, not by the order the budget dropped them) and `over_by` when pinned blocks exceed the cap. `ContextBuildEvent.dropped` in Python reads the list |
| `metadata` | What the `context_build` callbacks returned as `metadata`, merged, a later callback's key winning; `{}` without any. On a turn whose named `tool_choice` the request did not offer, or whose `required` choice a hook left with no tool, `tool_choice_dropped` holds the tool's name (`required` for that choice) |

### `UsageEvent` (`usage`)

Token usage of one model call. See [Usage and cost](#usage-and-cost).

| Field | What it is |
|---|---|
| `usage` | This call: `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens` |
| `cumulative_by_model` | `cumulative` split by the model that served each call (model id to usage); its sum is `cumulative`; empty from a server that predates the field |
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
| `metadata` | Details for the host only, never shown to the model: what a host tool returned in [`ToolOutput.metadata`](./tools.md#metadata-for-the-host), or where an offloaded result went; for a failed call, the error's `details` object (a `Bash` command's `status` and `exit_code`, see [Workspace tools](./tools.md#workspace-tools)); `None` for a tool that set none |
| `error_code` | A failed call's machine-readable code (`invalid_input`, `not_found`, ...), when its tool gave one; `None` otherwise. `tool_interrupted` marks the stand-in end of a call whose start was durable when a crash came: its effect is unknown and it is never run again ([Runs: Tools run at most once](./runs.md#tools-run-at-most-once-never-exactly-once)) |
| `cache_hit` | Whether [`spec.tool_cache`](./tools.md#tool_cache) served the result: the tool did not run, and the call's `tool_call_start` is written with this event, not before it. `False` otherwise |
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
| `status` | `awaiting_approval`, or `awaiting_input` for `AskUser` questions |
| `pending_request_ids` | The `tool_use_id`s to answer with `agent.approve`, or with `agent.answer` when `awaiting_input` |

### `InputRequestEvent` (`input_request`)

An `AskUser` call waits for the user: one per call, before the `suspended`
event of status `awaiting_input`. See [Tools](./tools.md#ask-the-user).

| Field | What it is |
|---|---|
| `request_id` | The call's `tool_use_id`, the key of its `Answer` in `agent.answer` |
| `kind` | `ask_user` |
| `prompt` | The questions' texts, one per line |
| `questions` | The questions, each `{"id", "header", "question", "options", "multi_select"}`, an option being `{"label", "description"}` |

### `CompactionEvent` (`compaction`)

The view the model sees was shortened
([`spec.compaction`](./agent.md#compaction)): `messages_before`,
`messages_after`, `tokens_before`, `tokens_after` (estimates), `measured_tokens`
(what the trigger measured the view at, when that was more than `tokens_before`:
the provider's last reported prompt tokens plus the estimate of what followed
them; absent until a provider has reported usage), `strategy`
(`summary`, `micro` when old tool results were cleared with no model
call, or `clip` when the tool results of a refused prompt's last turn were
clipped to a budget), `clipped_bytes` (the bytes clipped out of text to make a
prompt fit, markers counted: what a forced summary request read less of the
older part, or what a `clip` took out of the view; absent when nothing was
clipped), `duration_ms`, the time from the decision to compact to this event
(the summary call and the projection), and `forced`, true when the
provider's refusal of a prompt as too long caused it
([Memory](./memory.md#when-the-prompt-does-not-fit)). `rejected` is set when
the summary was not applied and the view is unchanged (the counts agree). It
starts with a code, `tool_call` (the reply called a tool), `empty`,
`context_length` (a forced summary request was refused as longer than the
window: the run asks again on half as much of the older part, or ends when it
has made four requests), or
`not_smaller` (the view it would leave is estimated no smaller than the view now), then says what
follows: at a turn's start the run goes on and the next due turn asks again (from
the second rejection in a row, once the view has grown by a tenth of the
context window or the provider refuses the prompt); a forced summary ends the
run. An applied summary has no `rejected`, even when its `tokens_after` is still
at or above the trigger: such summaries, 2 in a row, hold the next asks as rejections do
([Memory](./memory.md#compaction)). `not_smaller` is judged on the whole view with the checkpoint in it (the same `tokens_before` and `tokens_after` the event reports), not the model's text, so an accepted compaction always has `tokens_after < tokens_before`.

### `LoopGuardEvent` (`loop_guard`)

The run hit its turn limit or the [loop guard](./agent.md#loop_guard)
tripped: `reason` (`max_turns`, `repeated_tool_call`, `repeated_failures` or
`idle_turns`), `detail` (for example `Maximum turns (20) reached.` or
`Tool 'Read' called with identical inputs and the same result 3 time(s)
(limit: 3).`) and
`action` (`stop`). An `error` event (`TurnLimitError` or `LoopGuardError`)
and a `result` of subtype `error` follow.

### `ToolRetryEvent` (`tool_retry`)

A tool attempt failed with an error [`spec.tool_retry`](./tools.md#tool_retry)
retries, and the call runs again after a wait: `tool_use_id`, `tool_name`,
`attempt` (the one about to run, from 2), `max_attempts`, `error_class` (the
error's `code`, or `timeout`), `error` (the failed attempt's message, run
through `spec.redaction` when it covers tool results and then cut to 512
bytes) and `delay_ms`. It is durable before the wait.

### `ToolInterruptedEvent` (`tool_interrupted`)

A call that started and whose end a crash lost: `tool_use_id` and
`tool_name`. A resume writes it with the `tool_call_end` that stands in for
the lost one (an error whose `error_code` is `tool_interrupted`, and whose
text tells the model the effect is unknown), then does not run the call
again. Its `tool_use_id` is the same after the resume: the key to ask the
system the tool talks to whether the effect landed ([Runs: Tools run at most
once](./runs.md#tools-run-at-most-once-never-exactly-once)). An older client
reads it as an unknown event.
### `PolicyEvent` (`policy`)

The [reliability policy](./agent.md#policy) decided. `rule` is
`prerequisite` or `completion`. A prerequisite refusal (`action` `refuse`)
names the `tool` and `tool_use_id` of the call that was answered with an
error and not run, and the tools in `missing` that have not succeeded yet.
A completion decision names the `gates` the final answer failed and the
tools they still need (`missing`): `correct` sends the answer back to the
model for another turn, with `attempt` the corrections spent so far, and
`stop` ends the run `failed` (an `error` named `PolicyGateError` and a
`result` of subtype `error` follow). `detail` is what the model was told.

### `ProviderRetryEvent` (`provider_retry`)

A provider call failed with a retryable error and the run calls the same
model again ([`spec.provider_retry`](./providers.md#failures-and-retries)):
`model`, `retry` (this model's retry in the turn, from 1), `delay_ms` (the
wait before the next call) and `error` (the failure, shaped as an
`ErrorEvent`'s `error`). It arrives before the wait. Discard the partial text
of the failed call: the next call answers from the start.

### `ModelFallbackEvent` (`model_fallback`)

The run gave up on a model and calls the next of
[`spec.fallback_models`](./providers.md#failures-and-retries) for the rest of
the run: `from_model`, `to_model` and `error` (the failure that ended
`from_model`'s calls). Python Linch's event has a `reason` string in place of
`error`. As for `provider_retry`, discard the partial text of the failed call:
the fallback answers from the start.

### `VerificationEvent` (`verification`)

A check on the final answer found it wanting, or ran out of retries:
`verifier`, `action` (`retry`, `stop` or `exhausted`), `feedback` and
`attempt`. The [output schema](./structured-output.md#what-the-run-checks)
check emits it with `verifier` `output_schema`; a
[verifier](./security.md#verifiers) with its `name`; a `stop` callback's
retry under `spec.verification` with its `policy_id`.

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

### `CacheDiagnosticEvent` (`cache_diagnostic`), stream-only

Why the provider's prompt cache read what it did on a call, against the run's
previous call. It is sent only when the agent's
[`monitoring.cache_diagnostic`](./providers.md#find-out-why-a-cache-missed) is
on, never stored, never replayed and never part of the run contract. With it
off, nothing is hashed and the stream, the log and the writes are exactly what
they were.

| Field | What it is |
|---|---|
| `reason` | The first check that holds, in this order: `model_changed`, `provider_changed`, `ttl_expired` (more time than the provider keeps an entry: Anthropic's 5 minutes, or 1 hour when the request asks; no other adapter declares one), `system_changed`, `tools_changed`, `compaction` (the conversation got shorter), `prefix_changed` (an earlier message differs), `low_read` (the prefix is the same, yet the provider read under half of the previous call's input tokens) or `none` |
| `previous_cached` | Tokens the previous call read from the cache |
| `cached` | Tokens this call read from the cache |
| `prompt_tokens` | Input tokens of this call, cached and not |
| `first_changed_message` | Index of the first message that differs from the previous request's, or `None` |

One event follows each provider call that reports input tokens, from the
second call of a run call on (a resumed run has nothing in memory to compare
with, so its first call says nothing). A call under 1024 input tokens (2048 for
OpenAI) says nothing, since no provider caches less. The event holds counts
and an index, never text or a hash.

### `ErrorEvent` (`error`)

An error the run recorded. `error` is a dict with `name` and `message`, and
`retryable`; a provider failure also has `kind` (`connect`, `transport`, `status`,
`context_length`, `invalid_request`, `stream`) and, for an HTTP failure, `status`:

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
| `usage_by_model` | `total_usage` split by serving model (model id to usage; a fallback or a compaction summary shows as its own entry); its sum is `total_usage` |
| `subagent_usage` | Each subagent child run's own total, by child run id (`SubagentUsage`: `total_usage`, `usage_complete`, `usage_by_model`, `total_cost_usd`, `subagent_type`); not part of `total_usage`, so the tree is `total_usage` plus each entry |
| `duration_ms` | Wall time of the `run`, `resume` or `approve` call that ended the run. The whole run's span is this envelope's `ts_ms` minus the first `init` envelope's (the one not `resumed`) |
| `total_cost_usd` | Cost of the run's priced calls; `None` when none was priced |
| `structured_output`, `structured_error` | With an [output schema](./structured-output.md): the parsed answer, or why the last answer failed the schema (the run then ends `error`); `None` without one |
| `trace_id` | The run's `trace_id`, as on `InitEvent` |

### `SkillsLoadedEvent` (`skills_loaded`)

The agent's skills, on a session's first run of an agent with skills,
between `init` and the prompt's `user` event. A run that does not offer the
`Skill` tool (a subagent whose definition leaves it out) sends none.
`skills` holds one dict per skill, sorted by name: the built-in `verify`
included, and, as in Python, a skill with `disable_model_invocation` that
the model may not load and the prompt's listing leaves out. Each has
`name`, `description`, and `when_to_use` and `argument_hint` when the skill
sets them. Python Linch sends it once per session object, so again after a
restart; here a session's later runs never do.

### `SkillInvokedEvent` (`skill_invoked`)

A `Skill` call is about to start, just before its `tool_call_start`.
`name` is the skill and `args` the argument text it was given, or `None`. `model_override` and `allowed_tools` are
always `None` today. See [Skills](./skills.md).

### `SkillCompletedEvent` (`skill_completed`)

That skill's call ended, just after its `tool_call_end`: `name`, and
`is_error` (`True` when the call failed: the skill could not be loaded, or
the call was denied).

### `SubagentEvent` (`subagent`)

An event of a subagent's child run.

| Field | What it is |
|---|---|
| `child_run_id` | The child run: `<parent run id>:<tool_use_id>` of the call that runs it |
| `tool_use_id` | The parent's call that runs the child: the `Subagent` call, or the `SubagentContinue` call that continues a worker |
| `ts_ms` | When the child emitted the event, from the child's own envelope. A background child's events reach the parent's log at its next turn, so the outer envelope's `ts_ms` is later |
| `event` | The child's event, decoded into its own class (an `InitEvent`, a `ToolCallEndEvent`, ...). Never another `SubagentEvent`: a child cannot spawn |

A foreground subagent's events, its stream-only ones included, reach the
parent's stream this way; a background worker's durable ones do. See
[Subagents](./subagents.md).

### `ChildRunStatusEvent` (`child_run_status`)

A background worker's child run changed status: `child_run_id` and `status`
(`running`, then `completed`, `failed` or `aborted`).

### `ScheduleFireEvent` (`schedule_fire`)

A worker started this run for a [schedule](./scheduler.md#trace-a-run-to-its-schedule)
fire: `schedule_id` and `fire_at_ms`, when the fire was due in Unix
milliseconds. It follows the run's first `init` in the same write; a run no
schedule started, a queued job and a resume have none. Its envelope is
written `ignorable`, so a Rust build that predates the kind skips it when it
reads the run's events instead of failing on an unknown type.

### Journal-only kinds

`ModelCallDeliveryEvent` (`model_call_delivery`), `ModelCallReceiptEvent`
(`model_call_receipt`) and `ModelCallUsageSettlementEvent`
(`model_call_usage_settlement`) are the accounting journal's records. The
server writes them to the journal and neither streams them nor returns them
from `Agent.events`, so the client decodes them only from envelopes read from
a journal. `ModelCallDeliveryEvent` has `logical_call_id`,
`delivery_attempt_id`, `kind` (`primary` or `compaction`), `requested_model`
and `turn_index`; `ModelCallReceiptEvent` the same ids and `kind` with
`disposition` (`accepted`, `rejected`, `cancelled`, `failed` or
`not_dispatched`), `duration_ms`, `actual_model`, `usage` and `cost_usd`;
`ModelCallUsageSettlementEvent` `logical_call_id`, `usage`, `budget_owner_id`
and `cost_usd`.

`SteerReceivedEvent` (`steer_received`) is a [steer](./runs.md#steer) the run
accepted, with `steer_id` and `text` (after prompt redaction). It is written
before `steer` answers and never streamed; the `user` event of subtype
`alignment` with the same `steer_id` marks it delivered to the model.

### Kinds the server does not emit yet

The core declares these kinds and the client decodes each into its class,
but no run emits them today:

| Class (`type`) | Fields |
|---|---|
| `PromptCacheAdvisoryEvent` (`prompt_cache_advisory`) | `reason` (`tool_set_changed` or `model_changed`), `detail` |
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
