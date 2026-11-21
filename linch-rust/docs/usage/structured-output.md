# Structured output

[Tiếng Việt](../vi/usage/structured-output.md)

Use an output schema when the caller needs the run's final answer as data: a
JSON object that satisfies a JSON Schema you give. The run checks the answer,
sends a failing one back to the model with the error, and ends with the parsed
object in `ResultEvent.structured_output`.

---

## Set a schema on the agent

The schema goes in `spec.output_schema`; `spec.structured_output_retries`
bounds how many failing answers go back to the model (0 when absent).

```python
from linch_rust import AgentConfig, OutputSchema, Server

invoice: OutputSchema = {
    "name": "invoice",
    "schema": {
        "type": "object",
        "properties": {"total": {"type": "number"}, "currency": {"type": "string"}},
        "required": ["total", "currency"],
        "additionalProperties": False,
    },
}

config: AgentConfig = {
    "namespace": "billing",
    "spec": {
        "model": "claude-sonnet-4-6",
        "output_schema": invoice,
        "structured_output_retries": 2,
    },
    "provider": {"id": "anthropic"},
}

async with Server() as server:
    agent = await server.open_agent(config)
    run = await agent.run("s1", "Bill 3 widgets at 4 EUR each.")
    result = await run.result()
    print(result.structured_output)  # {'total': 12, 'currency': 'EUR'}
```

| `OutputSchema` field | Default | What it controls |
|---|---|---|
| `name` | required | Identifier sent to the provider; not blank |
| `schema` | required | A JSON Schema (draft 2020-12) whose root is `{"type": "object", ...}` |
| `strict` | `True` | Whether a provider that can enforce the schema exactly should (OpenAI) |
| `description` | none | What the answer is, sent to providers that take one |

`agent.open` refuses a schema that does not compile, one whose root `type`
is not `object`, and a blank `name` (`OpenError`, code -32011). The schema and
the retries are part of the [run contract](./agent.md#the-run-contract); an
agent without them keeps the contract it had.

## Give one run its own schema

`agent.run(..., output_schema=...)` replaces the agent's schema for that run
only. The agent's `structured_output_retries` still bound it, so an agent
whose runs bring their own schema may set the retries without a schema of
its own.

```python
person: OutputSchema = {"name": "person", "schema": {"type": "object", "required": ["name"]}}
run = await agent.run("s2", "Who wrote the Analytical Engine notes?", output_schema=person)
```

The run stores the schema in its first `init` event (`InitEvent.output_schema`),
so a `resume` or `approve` uses it again, from any process. A schema that does
not compile, or whose root is not an object, ends the run with
`InvalidOutputSchema` (code -32031) before anything is written: no run or
session exists. A per-run schema does not change the contract.

## What the run checks

Only the final answer is checked: a response without tool calls, after any
background subagents reported. Tool calls and their results are never
checked.

1. The text is read as JSON; when it is not JSON, the first fenced block
   (```` ```json ```` or a bare ```` ``` ````) is read instead.
2. The value must be a JSON object, then satisfy the schema.

A failing answer is kept in the session, and a `verification` event
(`verifier: "output_schema"`, `action: "retry"`, `attempt` from 1) records
it. The model then gets this, in a `<system-reminder>` user message, and
answers again:

```text
Your previous response failed structured-output validation: <error>
Respond again with ONLY a JSON object matching the required schema.
```

`<error>` is `JSON parse error: ...`, `Expected a JSON object, got array`,
`JSON nests deeper than 64 levels`, or `Schema validation error:` followed by
one `- path: message` line per violation.

When the retries are spent, or with `structured_output_retries` 0, the next
failing answer ends the run: a `verification` event with `action: "stop"`,
an `error` named `StructuredOutputError`, and a `result` of subtype `error`
whose `structured_error` is the last error and whose `final_text` is the
model's text. `max_turns` still applies, so it can end the run first.

The retry count is durable: a run that crashed and resumed counts the retries
it already spent from its events, so a crash never grants more.

## With callbacks

The check runs before the `stop` callbacks, so they only see an answer that
satisfies the schema, and their request has `structured_output`, the parsed
object, beside `text`.

| `stop` answer | What happens |
|---|---|
| `allow` | The run ends with that answer |
| `rewrite` | The new text is checked again. A valid rewrite is the result; an invalid one ends the run failed with `StructuredOutputError`, without asking the model again |
| `retry`, `continue` | The model answers again, and that answer is checked from the start. These do not spend `structured_output_retries` |
| `block` | The run fails, as without a schema |

## What each provider receives

On a provider with native structured output, every request of the run, the
summary call of a compaction excluded, carries the schema. Each adapter sends
it in the vendor's own structured-output form, which shapes only the text
answer, so the model can still call tools on any turn. The others get a
[final tool](#the-final-tool) instead:

| Provider | Sent as |
|---|---|
| `anthropic` | `output_config.format` = `{"type": "json_schema", "schema": ...}` |
| `openai-chat` | `response_format` = `{"type": "json_schema", "json_schema": {name, strict, schema, description?}}` |
| `openai-responses` | `text.format` = `{"type": "json_schema", name, strict, schema, description?}` |
| `gemini` | `generationConfig.responseMimeType` `application/json` and `responseJsonSchema` |
| `deepseek`, `vllm`, `sglang`, `llamacpp` | Not sent: the run offers a [final tool](#the-final-tool) instead |

Vendors accept only part of JSON Schema in these forms (Anthropic and OpenAI
strict mode want `additionalProperties: false` on every object, for example).
The schema is sent as you wrote it; the run still checks every answer itself,
so a provider that drifts from the schema gets the retry above. Gemini
combines a JSON response with function calling from Gemini 3 on. These
request shapes follow each vendor's API docs; they have not yet been run
against a live API.

A run that fails over to one of `spec.fallback_models` sends the fallback the
same schema, checks its answers the same way, and keeps counting the schema
retries the run already spent.

## The final tool

DeepSeek has no JSON-schema response format (only `json_object`, which does
not carry the schema), and the vLLM, SGLang and llama.cpp adapters send none.
On these providers the run sends no schema. It offers one more tool, named
after the schema (`name`), whose input schema is your `schema` and whose
description is yours or a sentence asking for the final answer. It goes
last in the tool list, so the cached prefix of the others keeps its bytes.

A response that calls it ends the run with the call's input as the answer:

- The call never runs: there is no `tool_call_start` or `tool_call_end`. It
  is decided like any call, though: the tool's input schema and validation,
  the `pre_tool_use` callbacks and the permission rules apply, and the
  `hook` events of the callbacks are emitted. A callback may rewrite the
  input (the answer is the rewritten one), a veto or a deny rule goes back
  as the call's error result, and an input the tool does not accept does too.
  These refusals draw on `structured_output_retries`, so one that never lifts
  ends the run `failed` with `StructuredOutputError` instead of looping, even
  under a forced `tool_choice` with `max_turns` unset. An `Ask` decision
  counts as allowed: nothing runs, so there is nothing to approve.
- Before the call is decided, a [background subagent](./agent.md) still
  running, or one that ended undelivered, is waited for as for a text answer.
  The call's result then says the answer was not taken, the subagent's
  notification rides in the same message, and the model calls the tool again.
- Its input is checked like a text answer: a failure goes back as the call's
  error result (`Call the invoice tool again, ...`), under the same
  `structured_output_retries` and `verification` events, and past them the
  run fails with `StructuredOutputError`.
- It must be the only call of its response. One beside other calls is such a
  failure, and none of the calls runs.
- An input that passes meets the verifiers and the `stop` callbacks, which
  see it as JSON text; that text is the result's `final_text`, the object its
  `structured_output`. A callback's retry goes back as the call's result. A
  `stop` callback that rewrites the text is checked again: it must still be a
  JSON object that the output schema (if any) and the tool accept, and it
  becomes the `structured_output`; one that is not ends the run `failed`.
- The session gets a result for every call of the response before the run
  ends (`Final answer captured by the host.` for an answer taken), so the next
  run of the session starts paired, whatever ended this one. A crash after
  the decision finishes the run on resume without asking the model or a
  callback again.
- It ends the run once nothing is left to hear: a subagent started after
  the model's last turn is cancelled, as at any end of a run.

When the final tool is the only tool, it is forced with a named
`tool_choice`, or `required` where the provider refuses a named one, as
Python's compatible mode does. DeepSeek refuses a forced choice while it
thinks (the default), so set `spec.thinking` to `{"type": "disabled"}` to
force it there; otherwise the model is only offered the tool. A text answer
is still checked as above, so a model that answers in JSON text instead ends
the run too. Your own `spec.tool_choice` always wins.

The run records the tool's name in its first `init` event
(`InitEvent.final_tool`) and a resume uses it again. An agent whose own
schema becomes a final tool has it in its [run contract](./agent.md#the-run-contract);
an agent on a provider with native structured output keeps the contract it
had. `agent.open` refuses a schema whose `name` is a registered tool's or is
not a legal tool name (1 to 64 letters, digits, `_` or `-`).

### Name your own final tool

`spec.final_tool_name` names a registered tool whose call ends the run the
same way, on any provider (Python's `final_tool_name`). Its input is checked
against the tool's own input schema and `validate`, then against the output
schema when there is one, and is the answer as it is after that. The tool
never runs. `agent.open` refuses a name that is not a
registered tool.

```python
spec = {"model": "deepseek-v4-flash", "final_tool_name": "submit_report"}
```

## Choose which tool the model calls

`spec.tool_choice` sets whether and which tool the model must call, for every
request of the agent's runs; `agent.run(..., tool_choice=...)` replaces it
for one run, and the run stores it in its first `init` event
(`InitEvent.tool_choice`) so a resume keeps it.

| `tool_choice` | The model |
|---|---|
| `{"type": "auto"}` | Chooses: a tool or an answer |
| `{"type": "none"}` | Calls no tool |
| `{"type": "required"}` | Calls some tool |
| `{"type": "tool", "name": "Read"}` | Calls `Read` |

It is sent with every request but on a turn that offers no tool (Python's
rule: a skill that narrows a turn to no tool, for example). A forced choice holds every turn, so such a run can end only through
its final tool, or `max_turns` and the [loop guard](./agent.md#loop_guard).
A named tool is checked against each turn's own request, after any
`before_provider_call` rewrite: on a turn that does not offer it (a skill
narrowed the tools, tool search deferred it) the request goes out as `auto`
instead of failing with the provider's 400, and that turn's `context_build`
event has `metadata.tool_choice_dropped` with the tool's name. `required` is
dropped only on a turn a hook left with no tool at all (it cannot be satisfied
then): the request goes out with no choice, and `tool_choice_dropped` is
`"required"`. A named tool dropped on a turn with no tool goes out with no
choice too, not `auto`.
`agent.open` refuses a named tool the agent does not offer (the final tool
counts), `required` when it offers no tool at all, and a choice its provider
refuses; a run's own one is refused with
`InvalidToolChoice` (code -32034) before anything is written.

| Provider | Refuses |
|---|---|
| `deepseek` | `required` and a named tool while thinking is on, which is any `spec.thinking` but `{"type": "disabled"}` ([API reference](https://api-docs.deepseek.com/api/create-chat-completion)) |
| `anthropic` | `required` and a named tool with budgeted thinking (`{"type": "enabled", "budget_tokens": ...}`) |
| `llamacpp` | A named tool, which the server does not honour (`common/chat.cpp` parses only `auto`, `none` and `required`) |
| `vllm`, `sglang` | Nothing ([vLLM tool calling](https://docs.vllm.ai/en/latest/features/tool_calling/), [SGLang tool parser](https://docs.sglang.io/advanced_features/tool_parser.html)); a forced choice needs the server's tool-call parser |
| `openai-chat`, `openai-responses`, `gemini` | Nothing |

`tool_choice` and `final_tool_name` are part of the run contract; an agent
that sets neither keeps the contract it had. A subagent's runs take neither.

## How it differs from Python Linch

| Python Linch | linch_rust |
|---|---|
| Past the retries the run succeeds with `structured_error` set | The run fails (`result` subtype `error`) with `structured_error` set |
| The retry count restarts on resume | The retry count is read back from the run's events |
| `verification` with `action: "exhausted"` | `action: "stop"` |
| Anthropic `compatible` mode forces a final tool | The final tool is offered by every provider without native structured output (DeepSeek, vLLM, SGLang, llama.cpp); Anthropic always uses its native form |
| The final tool is acknowledged, then a schema failure goes back as a separate message | A schema failure is the final call's error result |
| A final call beside other calls fails the run | It is a schema failure, retried under `structured_output_retries` |
| A per-run `final_tool_name` | Only `spec.final_tool_name` |

## Related pages

- [Agent config](./agent.md#spec): the rest of `spec`
- [Runs](./runs.md#errors-arrive-at-the-end-of-the-stream): `InvalidOutputSchema`, `InvalidToolChoice` and the other errors
- [Events](./events.md): `VerificationEvent` and `ResultEvent`
- [Architecture: turn lifecycle](../architecture/turn-lifecycle.md#the-loop-continues-while-a-response-has-tool-calls): where the check runs and what it writes
